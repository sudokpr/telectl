from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from image_summary import ImageSummaryConfig
from image_summary import build_config as build_image_summary_config
from image_summary import log as image_summary_log
from image_summary import process_image, split_message


BASE_DIR = Path(__file__).resolve().parent
RUN_DIR = BASE_DIR / "run"
LOG_DIR = BASE_DIR / "logs"
PID_FILE = RUN_DIR / "codex-remote-control.pid"
LOG_FILE = LOG_DIR / "codex-remote-control.log"


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: int
    topic_id: int
    allowed_user_ids: frozenset[int]
    command: str
    workdir: Path
    image_summary: ImageSummaryConfig


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def first_value(names: Iterable[str], *sources: dict[str, str]) -> str | None:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    for source in sources:
        for name in names:
            if source.get(name):
                return source[name]
    return None


def parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


def load_config() -> Config:
    local_env = load_dotenv(BASE_DIR / ".env")
    fallback_env: dict[str, str] = {}
    fallback_env_path = first_value(["TELEGRAM_ENV_PATH", "JOURGRAM_ENV_PATH"], local_env)
    if fallback_env_path:
        fallback_env = load_dotenv(Path(fallback_env_path).expanduser())

    bot_token = first_value(["BOT_TOKEN", "TELEGRAM_BOT_TOKEN"], local_env)
    chat_id = first_value(["TELEGRAM_CHAT_ID", "TELEGRAM__CHAT_ID"], local_env, fallback_env)
    topic_id = first_value(["TELEGRAM_TOPIC_ID", "TELEGRAM__TOPIC_ID"], local_env)
    command = first_value(["CODEX_REMOTE_COMMAND"], local_env) or "codex remote-control"
    workdir = Path(
        first_value(["CODEX_REMOTE_WORKDIR"], local_env)
        or str(BASE_DIR)
    ).expanduser()

    missing = [
        name
        for name, value in {
            "BOT_TOKEN": bot_token,
            "TELEGRAM_CHAT_ID": chat_id,
            "TELEGRAM_TOPIC_ID": topic_id,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required config: {', '.join(missing)}")

    image_summary_env = {**local_env, **dict(os.environ)}

    return Config(
        bot_token=bot_token or "",
        chat_id=int(chat_id or "0"),
        topic_id=int(topic_id or "0"),
        allowed_user_ids=parse_user_ids(first_value(["ALLOWED_USER_IDS"], local_env)),
        command=command,
        workdir=workdir,
        image_summary=build_image_summary_config(image_summary_env, int(chat_id or "0")),
    )


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def append_log(command: str, result: subprocess.CompletedProcess[str]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] $ {command}\n")
        if result.stdout:
            log.write(result.stdout)
        if result.stderr:
            log.write(result.stderr)
        log.write(f"[exit {result.returncode}]\n")


def run_shell_command(command: str, workdir: Path, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    workdir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["/bin/bash", "-lc", command],
        cwd=workdir,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    append_log(command, result)
    return result


def remote_command(config: Config, subcommand: str) -> str:
    return f"{config.command} {subcommand}"


def daemon_version_command(config: Config) -> str:
    parts = shlex.split(config.command)
    if not parts:
        raise RuntimeError("CODEX_REMOTE_COMMAND is empty")
    return f"{shlex.quote(parts[0])} app-server daemon version"


def start_process(config: Config) -> subprocess.CompletedProcess[str]:
    RUN_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    return run_shell_command(remote_command(config, "start"), config.workdir)


def stop_remote_control(config: Config) -> subprocess.CompletedProcess[str]:
    PID_FILE.unlink(missing_ok=True)
    return run_shell_command(remote_command(config, "stop"), config.workdir)


def check_daemon(config: Config) -> subprocess.CompletedProcess[str]:
    return run_shell_command(daemon_version_command(config), config.workdir)


def command_summary(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    if not output:
        output = "(no output)"
    if len(output) > 1200:
        output = output[-1200:]
    return output


def unauthorized_reason(update: Update, config: Config) -> str | None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return "Unsupported update."
    if chat.id != config.chat_id:
        return "This command is only enabled in the configured supergroup."
    if message.message_thread_id != config.topic_id:
        return "This command is only enabled in the configured topic."
    if config.allowed_user_ids and (not user or user.id not in config.allowed_user_ids):
        return "You are not allowed to run this command."
    return None


async def guarded(update: Update, config: Config) -> bool:
    reason = unauthorized_reason(update, config)
    if reason:
        if update.effective_message:
            await update.effective_message.reply_text(reason)
        return False
    return True


def is_image_summary_target(update: Update, config: Config) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return False
    return (
        chat.id == config.image_summary.chat_id
        and message.message_thread_id == config.image_summary.topic_id
    )


def message_debug_line(update: Update) -> str:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message:
        return "update_without_message"

    document = message.document
    reply = message.reply_to_message
    return (
        f"message_id={message.message_id} "
        f"chat_id={chat.id if chat else None} "
        f"thread_id={message.message_thread_id} "
        f"from_user_id={user.id if user else None} "
        f"text={message.text!r} "
        f"caption={message.caption!r} "
        f"photo={bool(message.photo)} "
        f"document_mime={document.mime_type if document else None} "
        f"reply_to_message_id={reply.message_id if reply else None} "
        f"reply_to_thread_id={reply.message_thread_id if reply else None}"
    )


async def debug_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    if config.image_summary.debug_updates:
        await asyncio.to_thread(image_summary_log, config.image_summary, f"debug {message_debug_line(update)}")


async def typing_loop(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    topic_id: int,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await context.bot.send_chat_action(
            chat_id=chat_id,
            message_thread_id=topic_id,
            action=ChatAction.TYPING,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


def image_suffix(update: Update) -> str:
    message = update.effective_message
    if not message:
        return ".img"
    if message.photo:
        return ".jpg"
    document = message.document
    if document and document.file_name:
        return Path(document.file_name).suffix or ".img"
    if document and document.mime_type:
        return {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(document.mime_type, ".img")
    return ".img"


async def download_image(update: Update, target: Path) -> None:
    message = update.effective_message
    if not message:
        raise RuntimeError("No message to download")
    if message.photo:
        telegram_file = await message.photo[-1].get_file()
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        telegram_file = await message.document.get_file()
    else:
        raise RuntimeError("Message does not contain a supported image")
    target.parent.mkdir(parents=True, exist_ok=True)
    await telegram_file.download_to_drive(custom_path=target)


async def reply_chunked(update: Update, text: str, max_chars: int) -> None:
    message = update.effective_message
    if not message:
        return
    chunks = split_message(text, max_chars)
    for index, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"({index + 1}/{len(chunks)})\n{chunk}"
        await message.reply_text(chunk)


async def image_summary_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    summary_config = config.image_summary
    message = update.effective_message
    if not message or not is_image_summary_target(update, config):
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = summary_config.work_dir / "images" / f"{stamp}-msg{message.message_id}{image_suffix(update)}"
    image_summary_log(summary_config, f"processing {message_debug_line(update)} target={target}")

    await message.reply_text(
        "Received the image. Processing OCR + direct vision summary now; this can take 1-2 minutes."
    )

    try:
        await download_image(update, target)
        image_summary_log(
            summary_config,
            f"downloaded message_id={message.message_id} path={target} bytes={target.stat().st_size}",
        )
    except Exception as exc:
        image_summary_log(summary_config, f"download_failed message_id={message.message_id} error={exc}")
        await message.reply_text(f"Failed to download image: {exc}")
        return

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(
        typing_loop(context, summary_config.chat_id, summary_config.topic_id, stop_event)
    )
    try:
        reply = await asyncio.to_thread(process_image, target, summary_config)
    finally:
        stop_event.set()
        await typing_task

    await reply_chunked(update, reply, summary_config.max_reply_chars)
    image_summary_log(summary_config, f"replied message_id={message.message_id}")


async def codex_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    if not await guarded(update, config):
        return

    try:
        stop_result = await asyncio.to_thread(stop_remote_control, config)
        start_result = await asyncio.to_thread(start_process, config)
        status_result = await asyncio.to_thread(check_daemon, config)
    except Exception as exc:
        await update.effective_message.reply_text(
            f"Failed to start codex remote-control: {exc}\nLog: {LOG_FILE}"
        )
        return

    if start_result.returncode == 0 and status_result.returncode == 0:
        await update.effective_message.reply_text(
            "Restarted codex remote-control daemon.\n"
            f"Command: {remote_command(config, 'start')}\n"
            f"Status: {command_summary(status_result)}\n"
            f"Log: {LOG_FILE}"
        )
        return

    await update.effective_message.reply_text(
        "Tried to restart codex remote-control, but the daemon status check failed.\n"
        f"Stop exit: {stop_result.returncode}\n"
        f"Start exit: {start_result.returncode}\n"
        f"Status exit: {status_result.returncode}\n"
        f"Command: {remote_command(config, 'start')}\n"
        f"Output: {command_summary(status_result)}\n"
        f"Log: {LOG_FILE}"
    )


async def codex_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    if not await guarded(update, config):
        return

    result = await asyncio.to_thread(stop_remote_control, config)
    await update.effective_message.reply_text(
        "Stopped codex remote-control daemon.\n"
        f"Exit: {result.returncode}\n"
        f"Output: {command_summary(result)}\n"
        f"Log: {LOG_FILE}"
    )


async def codex_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    if not await guarded(update, config):
        return

    result = await asyncio.to_thread(check_daemon, config)
    if result.returncode == 0:
        await update.effective_message.reply_text(
            "codex app-server daemon is running.\n"
            f"Command: {daemon_version_command(config)}\n"
            f"Status: {command_summary(result)}\n"
            f"Log: {LOG_FILE}"
        )
    else:
        await update.effective_message.reply_text(
            "codex app-server daemon is not reachable.\n"
            f"Command: {daemon_version_command(config)}\n"
            f"Exit: {result.returncode}\n"
            f"Output: {command_summary(result)}\n"
            f"Log: {LOG_FILE}"
        )


async def post_init(application: Application) -> None:
    config: Config = application.bot_data["config"]
    commands = [
        BotCommand("codex_start", "restart Codex remote control"),
        BotCommand("codex_status", "show Codex remote-control status"),
        BotCommand("codex_stop", "stop Codex remote control"),
    ]
    await application.bot.set_my_commands(
        commands,
        scope=BotCommandScopeChat(chat_id=config.chat_id),
    )


def main() -> None:
    config = load_config()
    application = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .build()
    )
    application.bot_data["config"] = config
    application.add_handler(CommandHandler("codex_start", codex_start))
    application.add_handler(CommandHandler("codex_status", codex_status))
    application.add_handler(CommandHandler("codex_stop", codex_stop))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_summary_handler))
    application.add_handler(MessageHandler(filters.ALL, debug_update), group=1)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
