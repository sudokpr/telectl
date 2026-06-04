from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from fuel_tracker import FuelApproval, FuelConfig, FuelPending, append_fuel_row, apply_corrections, apply_fill_type, build_fuel_config
from fuel_tracker import build_row as build_fuel_row
from fuel_tracker import extract_fuel_fields, format_approval, make_approval, parse_corrections
from image_summary import ImageSummaryConfig
from image_summary import build_result_reply, image_result_jobs
from image_summary import build_config as build_image_summary_config
from image_summary import log as image_summary_log
from image_summary import split_message
from http_intake import HttpIntakeConfig, build_http_config, start_http_intake
from memory_processor import answer_memory_question, save_memory


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
    http_intake: HttpIntakeConfig
    fuel: FuelConfig


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
        http_intake=build_http_config(image_summary_env),
        fuel=build_fuel_config(image_summary_env, int(chat_id or "0")),
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


def is_fuel_target(update: Update, config: Config) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if not config.fuel.enabled or not message or not chat:
        return False
    return chat.id == config.fuel.chat_id and message.message_thread_id == config.fuel.topic_id


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
        f"from_user_is_bot={user.is_bot if user else None} "
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
    cfg: ImageSummaryConfig,
    chat_id: int,
    topic_id: int,
    stop_event: asyncio.Event,
) -> None:
    image_summary_log(cfg, f"typing_loop_started chat_id={chat_id} topic_id={topic_id}")
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                message_thread_id=topic_id,
                action=ChatAction.TYPING,
            )
        except Exception as exc:
            image_summary_log(cfg, f"typing_loop_failed error={exc}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass
    image_summary_log(cfg, f"typing_loop_stopped chat_id={chat_id} topic_id={topic_id}")


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
        typing_loop(context, summary_config, summary_config.chat_id, summary_config.topic_id, stop_event)
    )
    try:
        for label, job in image_result_jobs(target, summary_config):
            image_summary_log(summary_config, f"image_result_started message_id={message.message_id} label={label!r}")
            result = await asyncio.to_thread(job)
            image_summary_log(
                summary_config,
                f"image_result_finished message_id={message.message_id} "
                f"label={label!r} ok={result['ok']} seconds={result['seconds']:.1f}",
            )
            await reply_chunked(update, build_result_reply(result, summary_config.summary_mode), summary_config.max_reply_chars)
    finally:
        stop_event.set()
        await typing_task

    image_summary_log(summary_config, f"replied message_id={message.message_id}")


def fuel_group_key(message: Any) -> str:
    if message.media_group_id:
        return f"album:{message.media_group_id}"
    return f"window:{message.chat_id}:{message.message_thread_id}"


def fuel_approval_keyboard(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Full tank", callback_data=f"fuel:full:{approval_id}"),
                InlineKeyboardButton("Partial", callback_data=f"fuel:partial:{approval_id}"),
            ],
            [
                InlineKeyboardButton("Correction", callback_data=f"fuel:correction:{approval_id}"),
                InlineKeyboardButton("Reject", callback_data=f"fuel:reject:{approval_id}"),
            ],
        ]
    )


def approval_id_from_text(text: str) -> str | None:
    match = re.search(r"Approval ID:\s*`?([0-9a-f]{12})`?", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\bapproval(?:_id)?\s*[:=]\s*([0-9a-f]{12})\b", text, re.IGNORECASE)
    return match.group(1) if match else None


def correction_key(message: Any) -> str:
    user_id = getattr(message.from_user, "id", None)
    return f"{message.chat_id}:{message.message_thread_id}:{user_id}"


def approval_id_for_correction(message: Any, pending_corrections: dict[str, tuple[str, float]]) -> str | None:
    now = asyncio.get_running_loop().time()
    key = correction_key(message)
    pending = pending_corrections.get(key)
    if pending:
        approval_id, expires_at = pending
        if now <= expires_at:
            return approval_id
        pending_corrections.pop(key, None)

    reply = message.reply_to_message
    if reply:
        reply_text = reply.text or reply.caption or ""
        approval_id = approval_id_from_text(reply_text)
        if approval_id:
            return approval_id

    own_text = message.text or message.caption or ""
    approval_id = approval_id_from_text(own_text)
    if approval_id:
        return approval_id
    return None


async def process_fuel_pending(
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    delay_seconds: int,
) -> None:
    await asyncio.sleep(delay_seconds)
    config: Config = context.application.bot_data["config"]
    pending_by_key: dict[str, FuelPending] = context.application.bot_data["fuel_pending"]
    pending = pending_by_key.pop(key, None)
    if not pending:
        return

    image_summary_log(
        config.image_summary,
        f"fuel_processing key={key} images={len(pending.image_paths)} messages={pending.message_ids}",
    )
    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(
        typing_loop(context, config.image_summary, config.fuel.chat_id, config.fuel.topic_id, stop_event)
    )
    try:
        extracted = await asyncio.to_thread(extract_fuel_fields, pending.image_paths, config.fuel)
        row = build_fuel_row(extracted, config.fuel, pending.image_paths, pending.message_ids)
        approval = make_approval(row, pending.image_paths, pending.message_ids)
        approvals: dict[str, FuelApproval] = context.application.bot_data["fuel_approvals"]
        approvals[approval.approval_id] = approval
        await context.bot.send_message(
            chat_id=config.fuel.chat_id,
            message_thread_id=config.fuel.topic_id,
            text=format_approval(row, config.fuel, approval.approval_id),
            reply_markup=fuel_approval_keyboard(approval.approval_id),
        )
        image_summary_log(config.image_summary, f"fuel_approval_sent id={approval.approval_id}")
    except Exception as exc:
        image_summary_log(config.image_summary, f"fuel_processing_failed key={key} error={exc}")
        await context.bot.send_message(
            chat_id=config.fuel.chat_id,
            message_thread_id=config.fuel.topic_id,
            text=f"Fuel extraction failed: {exc}",
        )
    finally:
        stop_event.set()
        await typing_task


async def fuel_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    if not message or not is_fuel_target(update, config):
        return

    key = fuel_group_key(message)
    pending_by_key: dict[str, FuelPending] = context.application.bot_data.setdefault("fuel_pending", {})
    pending = pending_by_key.get(key)
    now = asyncio.get_running_loop().time()
    if not pending:
        pending = FuelPending(key=key, created_at=now, updated_at=now)
        pending_by_key[key] = pending

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = config.fuel.work_dir / "images" / f"{stamp}-msg{message.message_id}{image_suffix(update)}"
    await download_image(update, target)
    pending.updated_at = now
    pending.message_ids.append(message.message_id)
    pending.image_paths.append(target)
    image_summary_log(
        config.image_summary,
        f"fuel_image_received key={key} message_id={message.message_id} path={target}",
    )

    if pending.task:
        pending.task.cancel()
    delay = 6 if message.media_group_id else config.fuel.pending_window_seconds
    pending.task = asyncio.create_task(process_fuel_pending(context, key, delay))
    await message.reply_text(
        f"Received fuel image {len(pending.image_paths)}. Waiting briefly for the matching receipt/odometer image."
    )


async def fuel_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "fuel":
        return
    action, approval_id = parts[1], parts[2]
    approvals: dict[str, FuelApproval] = context.application.bot_data.setdefault("fuel_approvals", {})
    approval = approvals.get(approval_id)
    if not approval:
        await query.answer("Approval expired or unknown.", show_alert=True)
        return

    if action == "correction":
        user = query.from_user
        message = query.message
        if not message or not user:
            await query.answer("Cannot start correction mode here.", show_alert=True)
            return
        pending_corrections: dict[str, tuple[str, float]] = context.application.bot_data.setdefault(
            "fuel_correction_pending",
            {},
        )
        key = f"{message.chat_id}:{message.message_thread_id}:{user.id}"
        expires_at = asyncio.get_running_loop().time() + config.fuel.correction_window_seconds
        pending_corrections[key] = (approval_id, expires_at)
        await query.answer("Send the correction values now.")
        await context.bot.send_message(
            chat_id=config.fuel.chat_id,
            message_thread_id=config.fuel.topic_id,
            text=(
                "Send correction values in the next "
                f"{config.fuel.correction_window_seconds // 60 or 1} minutes, for example:\n"
                "`odo=71234,vol=43,rate=112.0,amt=4533.20`"
            ),
        )
        image_summary_log(config.image_summary, f"fuel_correction_started id={approval_id} user_id={user.id}")
        return

    if action in {"approve", "full", "partial"}:
        row = apply_fill_type(approval.row, is_full=action != "partial")
        await asyncio.to_thread(append_fuel_row, row, config.fuel)
        approvals.pop(approval_id, None)
        pending_corrections: dict[str, tuple[str, float]] = context.application.bot_data.setdefault(
            "fuel_correction_pending",
            {},
        )
        for key, value in list(pending_corrections.items()):
            if value[0] == approval_id:
                pending_corrections.pop(key, None)
        fill_label = "full tank" if action != "partial" else "partial fill"
        await query.edit_message_text(
            f"Approved as {fill_label} and appended fuel entry to `{config.fuel.csv_path}`.\n\n"
            + format_approval(row, config.fuel, approval.approval_id)
        )
        await query.answer(f"Fuel entry saved as {fill_label}.")
        image_summary_log(
            config.image_summary,
            f"fuel_approved id={approval_id} fill={fill_label!r} csv={config.fuel.csv_path}",
        )
        return

    if action == "reject":
        approvals.pop(approval_id, None)
        pending_corrections: dict[str, tuple[str, float]] = context.application.bot_data.setdefault(
            "fuel_correction_pending",
            {},
        )
        for key, value in list(pending_corrections.items()):
            if value[0] == approval_id:
                pending_corrections.pop(key, None)
        await query.edit_message_text(f"Rejected fuel entry `{approval_id}`. CSV was not updated.")
        await query.answer("Fuel entry rejected.")
        image_summary_log(config.image_summary, f"fuel_rejected id={approval_id}")


async def fuel_correction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    if not message or not is_fuel_target(update, config):
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return

    raw_text = (message.text or message.caption or "").strip()
    if not raw_text or raw_text.startswith("/"):
        return

    approvals: dict[str, FuelApproval] = context.application.bot_data.setdefault("fuel_approvals", {})
    pending_corrections: dict[str, tuple[str, float]] = context.application.bot_data.setdefault(
        "fuel_correction_pending",
        {},
    )
    approval_id = approval_id_for_correction(message, pending_corrections)
    if not approval_id:
        return

    approval = approvals.get(approval_id)
    if not approval:
        await message.reply_text("That fuel approval is no longer pending.")
        return

    corrections = parse_corrections(raw_text)
    if not corrections:
        if "=" in raw_text or ":" in raw_text:
            await message.reply_text(
                "Correction not recognized. Use `odo=71234,vol=43,rate=112.0,amt=4533.20`."
            )
        return

    updated_row = apply_corrections(approval.row, corrections, config.fuel)
    updated_approval = FuelApproval(
        approval_id=approval.approval_id,
        row=updated_row,
        image_paths=approval.image_paths,
        message_ids=approval.message_ids,
    )
    approvals[approval_id] = updated_approval
    pending_corrections.pop(correction_key(message), None)

    changed = ", ".join(f"{field}={value}" for field, value in corrections.items())
    await message.reply_text(
        f"Updated pending fuel entry: {changed}\n\n"
        + format_approval(updated_row, config.fuel, approval_id),
        reply_markup=fuel_approval_keyboard(approval_id),
    )
    image_summary_log(config.image_summary, f"fuel_correction_applied id={approval_id} fields={list(corrections)}")


async def text_memory_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    summary_config = config.image_summary
    message = update.effective_message
    if not message or not is_image_summary_target(update, config):
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        image_summary_log(summary_config, f"memory_skipped_own_message {message_debug_line(update)}")
        return
    if message.photo or (message.document and message.document.mime_type.startswith("image/")):
        return

    raw_text = (message.text or message.caption or "").strip()
    if raw_text.startswith("/"):
        return
    if not raw_text:
        return

    image_summary_log(summary_config, f"memory_processing {message_debug_line(update)} chars={len(raw_text)}")
    await message.reply_text("Received text. Extracting key information and saving it as a memory.")

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(
        typing_loop(context, summary_config, summary_config.chat_id, summary_config.topic_id, stop_event)
    )
    try:
        saved = await asyncio.to_thread(
            save_memory,
            raw_text,
            summary_config,
            {
                "telegram_chat_id": message.chat_id,
                "telegram_thread_id": message.message_thread_id,
                "telegram_message_id": message.message_id,
                "telegram_date": message.date.isoformat() if message.date else None,
            },
        )
    finally:
        stop_event.set()
        await typing_task

    reply = f"Saved memory: `{saved.path}`\n\n{saved.content}"
    await reply_chunked(update, reply, summary_config.max_reply_chars)


async def answer_memory_query(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    config: Config = context.application.bot_data["config"]
    summary_config = config.image_summary
    message = update.effective_message
    if not message:
        return

    question = question.strip()
    if not question:
        await message.reply_text("Ask with `/memq your question` or `? your question`.")
        return

    image_summary_log(summary_config, f"memory_query question={question!r}")
    await message.reply_text(f"Searching memories with `{summary_config.memory_query_model}`.")

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(
        typing_loop(context, summary_config, summary_config.chat_id, summary_config.topic_id, stop_event)
    )
    try:
        result = await asyncio.to_thread(answer_memory_question, question, summary_config)
    finally:
        stop_event.set()
        await typing_task

    sources = "\n".join(f"- {path.name}" for path in result.context_paths)
    reply = result.answer
    if sources:
        reply += f"\n\nSources:\n{sources}"
    await reply_chunked(update, reply, summary_config.max_reply_chars)


async def memory_query_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    if not message or not is_image_summary_target(update, config):
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return
    await answer_memory_query(update, context, " ".join(context.args or []))


async def memory_query_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    if not message or not is_image_summary_target(update, config):
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return

    raw_text = (message.text or message.caption or "").strip()
    match = re.match(r"^(?:\?|q:|query:)\s*(.+)$", raw_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return
    await answer_memory_query(update, context, match.group(1))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config | None = context.application.bot_data.get("config")
    if config:
        await asyncio.to_thread(
            image_summary_log,
            config.image_summary,
            f"handler_error update={update!r} error={context.error!r}",
        )


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
        BotCommand("memq", "ask saved memories"),
    ]
    await application.bot.set_my_commands(
        commands,
        scope=BotCommandScopeChat(chat_id=config.chat_id),
    )
    server = start_http_intake(
        application,
        config.image_summary,
        config.http_intake,
        asyncio.get_running_loop(),
    )
    application.bot_data["http_intake_server"] = server


def main() -> None:
    config = load_config()
    image_summary_log(
        config.image_summary,
        "startup "
        f"chat_id={config.chat_id} codex_topic={config.topic_id} "
        f"image_topic={config.image_summary.topic_id} "
        f"memory_dir={config.image_summary.memory_dir} "
        f"ollama_url={config.image_summary.ollama_url}",
    )
    application = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .concurrent_updates(4)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["fuel_pending"] = {}
    application.bot_data["fuel_approvals"] = {}
    application.add_handler(MessageHandler(filters.ALL, debug_update), group=-1)
    application.add_handler(CallbackQueryHandler(fuel_callback_handler, pattern=r"^fuel:"))
    application.add_handler(CommandHandler("codex_start", codex_start))
    application.add_handler(CommandHandler("codex_status", codex_status))
    application.add_handler(CommandHandler("codex_stop", codex_stop))
    application.add_handler(CommandHandler(["memq", "memory_query"], memory_query_command))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, fuel_image_handler), group=0)
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_summary_handler), group=1)
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, fuel_correction_handler),
        group=1,
    )
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.Regex(r"^\s*(?:\?|q:|query:)"),
            memory_query_text_handler,
        )
    )
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, text_memory_handler)
    )
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
