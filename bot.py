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
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from fuel_tracker import FuelApproval, FuelConfig, FuelPending, append_fuel_row, apply_corrections, apply_fill_type, build_fuel_config
from fuel_tracker import build_row as build_fuel_row
from fuel_tracker import extract_fuel_fields, format_approval, make_approval, parse_corrections
from image_summary import ImageSummaryConfig
from image_summary import build_result_reply, image_result_jobs
from image_summary import build_config as build_image_summary_config
from image_summary import compare_ollama_to_codex
from image_summary import log as image_summary_log
from image_summary import processing_description
from image_summary import split_message
from http_intake import HttpIntakeConfig, build_http_config, start_http_intake
from memory_processor import answer_memory_question, save_memory
from owntracks.env import load_env as load_owntracks_env


BASE_DIR = Path(__file__).resolve().parent
from owntracks.digest import generate_digest as generate_owntracks_digest
from owntracks.env import project_path as owntracks_project_path
from owntracks.tagger import load_user_tags as load_owntracks_user_tags
from owntracks.tagger import save_user_tags as save_owntracks_user_tags
from owntracks.tagger import target_date_from_text as owntracks_target_date_from_text

RUN_DIR = BASE_DIR / "run"
LOG_DIR = BASE_DIR / "logs"
PID_FILE = RUN_DIR / "codex-remote-control.pid"
LOG_FILE = LOG_DIR / "codex-remote-control.log"

COMMAND_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("cmd", "list bot command shortcuts"),
    ("cxr", "restart Codex remote control"),
    ("cxs", "show Codex remote-control status"),
    ("cxq", "stop Codex remote control"),
    ("memq", "ask saved memories"),
    ("otd", "show OwnTracks activity digest"),
    ("otm", "send interactive OwnTracks map"),
    ("otb", "bulk-save OwnTracks stop reviews"),
    ("ott", "tag an OwnTracks stop"),
    ("otn", "name an OwnTracks stop"),
    ("oto", "add an OwnTracks stop note"),
    ("oth", "show OwnTracks help"),
)

OWNTRACKS_DATE_RE = re.compile(r"(?:today|yesterday|\d{1,2}|\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})")
OWNTRACKS_DATE_USAGE = "today|yesterday|DD|MM-DD|YYYY-MM-DD"


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: int
    topic_id: int
    allowed_user_ids: frozenset[int]
    command: str
    codex_remote_detached: bool
    workdir: Path
    image_summary: ImageSummaryConfig
    http_intake: HttpIntakeConfig
    fuel: FuelConfig
    owntracks_topic_id: int
    owntracks_user_tags_path: Path
    owntracks_map_delivery: str
    owntracks_map_base_url: str


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
        codex_remote_detached=(first_value(["CODEX_REMOTE_DETACHED"], local_env) or "true").strip().lower()
        in {"1", "true", "yes", "on"},
        workdir=workdir,
        image_summary=build_image_summary_config(image_summary_env, int(chat_id or "0")),
        http_intake=build_http_config(image_summary_env),
        fuel=build_fuel_config(image_summary_env, int(chat_id or "0")),
        owntracks_topic_id=int(first_value(["OWNTRACKS_TOPIC_ID"], local_env) or "0"),
        owntracks_user_tags_path=owntracks_project_path(
            first_value(["OWNTRACKS_USER_TAGS_PATH"], local_env),
            "./data/owntracks/user_tags.json",
        ),
        owntracks_map_delivery=(first_value(["OWNTRACKS_MAP_DELIVERY"], local_env) or "file").strip().lower(),
        owntracks_map_base_url=(first_value(["OWNTRACKS_MAP_BASE_URL", "HTTP_PUBLIC_BASE_URL"], local_env) or "").strip(),
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


def detached_remote_start_command(config: Config) -> str:
    return " ".join(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            "--unit=codex-remote-control",
            f"--working-directory={shlex.quote(str(config.workdir))}",
            "/bin/bash",
            "-lc",
            shlex.quote(remote_command(config, "start")),
        ]
    )


def daemon_version_command(config: Config) -> str:
    parts = shlex.split(config.command)
    if not parts:
        raise RuntimeError("CODEX_REMOTE_COMMAND is empty")
    return f"{shlex.quote(parts[0])} app-server daemon version"


def start_process(config: Config) -> subprocess.CompletedProcess[str]:
    RUN_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    if config.codex_remote_detached:
        return run_shell_command(detached_remote_start_command(config), config.workdir)
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


def is_allowed_user(update: Update, config: Config) -> bool:
    user = update.effective_user
    return not config.allowed_user_ids or bool(user and user.id in config.allowed_user_ids)


async def allowed_user_guard(update: Update, config: Config) -> bool:
    if is_allowed_user(update, config):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("You are not allowed to run this command.")
    return False


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
        f"Received the image. Processing {processing_description(summary_config)} summary now; this can take 1-2 minutes."
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
        results: list[dict[str, Any]] = []
        for label, job in image_result_jobs(target, summary_config):
            image_summary_log(summary_config, f"image_result_started message_id={message.message_id} label={label!r}")
            result = await asyncio.to_thread(job)
            results.append(result)
            image_summary_log(
                summary_config,
                f"image_result_finished message_id={message.message_id} "
                f"label={label!r} ok={result['ok']} seconds={result['seconds']:.1f}",
            )
            await reply_chunked(update, build_result_reply(result, summary_config.summary_mode), summary_config.max_reply_chars)

        image_summary_log(summary_config, f"image_comparison_started message_id={message.message_id}")
        comparison = await asyncio.to_thread(compare_ollama_to_codex, results, summary_config)
        if comparison:
            image_summary_log(
                summary_config,
                f"image_comparison_finished message_id={message.message_id} "
                f"ok={comparison['ok']} seconds={comparison['seconds']:.1f}",
            )
            await reply_chunked(update, build_result_reply(comparison, "comparison"), summary_config.max_reply_chars)
        else:
            image_summary_log(summary_config, f"image_comparison_skipped message_id={message.message_id}")
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
    if not await allowed_user_guard(update, config):
        return
    await answer_memory_query(update, context, " ".join(context.args or []))


async def memory_query_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    if not message or not is_image_summary_target(update, config):
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return
    if not await allowed_user_guard(update, config):
        return

    raw_text = (message.text or message.caption or "").strip()
    match = re.match(r"^(?:\?|q:|query:)\s*(.+)$", raw_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return
    await answer_memory_query(update, context, match.group(1))


async def command_shortcuts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["Bot command shortcuts:"]
    lines.extend(f"/{name} - {description}" for name, description in COMMAND_SHORTCUTS)
    lines.append("")
    lines.append("Long underscore commands may still work as hidden compatibility aliases.")
    await update.effective_message.reply_text("\n".join(lines))


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
        start_mode = "detached systemd user scope" if config.codex_remote_detached else "bot service process"
        await update.effective_message.reply_text(
            "Restarted codex remote-control daemon.\n"
            f"Mode: {start_mode}\n"
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


def owntracks_unauthorized_reason(update: Update, config: Config) -> str | None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return "Unsupported update."
    if chat.id != config.chat_id:
        return "OwnTracks commands are only enabled in the configured supergroup."
    if not config.owntracks_topic_id:
        return "OWNTRACKS_TOPIC_ID must be configured before OwnTracks commands are enabled."
    if message.message_thread_id != config.owntracks_topic_id:
        return "OwnTracks commands are only enabled in the OwnTracks topic."
    if config.allowed_user_ids and (not user or user.id not in config.allowed_user_ids):
        return "You are not allowed to run this command."
    return None


async def owntracks_guarded(update: Update, config: Config) -> bool:
    reason = owntracks_unauthorized_reason(update, config)
    if reason:
        if update.effective_message:
            await update.effective_message.reply_text(reason)
        return False
    return True


async def owntracks_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    date_text = context.args[0] if context.args else "today"
    try:
        plan, digest, _path = await asyncio.to_thread(generate_owntracks_digest, date_text)
    except Exception as exc:
        await update.effective_message.reply_text(f"Could not generate OwnTracks digest: {exc}")
        return
    remember_owntracks_date(update, context, plan["date"])
    for chunk in split_message(digest, 3600):
        await update.effective_message.reply_text(chunk, disable_web_page_preview=False)


def owntracks_map_url(config: Config, date_text: str) -> str:
    base_url = config.owntracks_map_base_url
    if not base_url:
        host = config.http_intake.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        base_url = f"http://{host}:{config.http_intake.port}"
    url = f"{base_url.rstrip('/')}/owntracks/map/{quote(date_text)}"
    if config.http_intake.token:
        url += "?" + urlencode({"token": config.http_intake.token})
    return url


async def owntracks_map_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    if context.args:
        date_text = context.args[0]
        if not OWNTRACKS_DATE_RE.fullmatch(date_text):
            await update.effective_message.reply_text(f"Usage: /otm [{OWNTRACKS_DATE_USAGE}]")
            return
    else:
        date_text = remembered_owntracks_date(update, context) or "today"
    if config.owntracks_map_delivery == "hosted":
        if not config.http_intake.enabled:
            await update.effective_message.reply_text("OWNTRACKS_MAP_DELIVERY=hosted requires HTTP_INTAKE_ENABLED=true.")
            return
        try:
            owntracks_env = load_owntracks_env()
            owntracks_tz = ZoneInfo(owntracks_env.get("OWNTRACKS_TIMEZONE", "Asia/Kolkata"))
            resolved_date = owntracks_target_date_from_text(date_text, owntracks_tz).isoformat()
        except Exception as exc:
            await update.effective_message.reply_text(f"Could not resolve OwnTracks map date: {exc}")
            return
        remember_owntracks_date(update, context, resolved_date)
        url = owntracks_map_url(config, resolved_date)
        await update.effective_message.reply_text(
            f"OwnTracks map for {resolved_date}:\n{url}",
            disable_web_page_preview=True,
        )
        return
    if config.owntracks_map_delivery not in {"file", "html", "attachment"}:
        await update.effective_message.reply_text("OWNTRACKS_MAP_DELIVERY must be 'file' or 'hosted'.")
        return
    try:
        plan, _digest, digest_path = await asyncio.to_thread(generate_owntracks_digest, date_text)
    except Exception as exc:
        await update.effective_message.reply_text(f"Could not generate OwnTracks map: {exc}")
        return
    remember_owntracks_date(update, context, plan["date"])
    map_path = digest_path.with_name(f"activity-map-{plan['date']}.html")
    if not map_path.exists():
        await update.effective_message.reply_text(f"OwnTracks map was not written: {map_path}")
        return
    caption = f"OwnTracks map for {plan['date']} with labeled stops."
    with map_path.open("rb") as handle:
        await update.effective_message.reply_document(
            document=handle,
            filename=map_path.name,
            caption=caption,
        )


def owntracks_session_key(update: Update) -> str:
    message = update.effective_message
    user = update.effective_user
    chat_id = message.chat_id if message else 0
    thread_id = message.message_thread_id if message else 0
    user_id = user.id if user else 0
    return f"{chat_id}:{thread_id}:{user_id}"


def remember_owntracks_date(update: Update, context: ContextTypes.DEFAULT_TYPE, date_text: str) -> None:
    context.bot_data.setdefault("owntracks_last_date", {})[owntracks_session_key(update)] = date_text


def remembered_owntracks_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.bot_data.setdefault("owntracks_last_date", {}).get(owntracks_session_key(update))


def owntracks_date_and_args(update: Update, context: ContextTypes.DEFAULT_TYPE, min_tail: int) -> tuple[str | None, list[str], str | None]:
    args = list(context.args)
    if args and OWNTRACKS_DATE_RE.fullmatch(args[0]):
        return args[0], args[1:], None
    remembered = remembered_owntracks_date(update, context)
    if remembered:
        return remembered, args, None
    if len(args) >= min_tail + 1:
        return args[0], args[1:], None
    return None, args, f"Run /otd {OWNTRACKS_DATE_USAGE} first, or include a date."


def resolve_owntracks_stop(date_text: str, stop_ref: str) -> tuple[dict, str]:
    plan, _digest, _path = generate_owntracks_digest(date_text)
    for stop in plan["candidate_stops"]:
        if stop_ref in {stop["id"], stop.get("alias")}:
            return stop, plan["date"]
    valid = ", ".join(f"{stop.get('alias')}={stop['id']}" for stop in plan["candidate_stops"])
    raise ValueError(f"Unknown stop '{stop_ref}'. Valid stops: {valid or 'none'}")


def resolve_owntracks_stop_id(date_text: str, stop_ref: str) -> tuple[str, str]:
    stop, target_date = resolve_owntracks_stop(date_text, stop_ref)
    return stop["id"], target_date


def annotate_saved_stop(stop_data: dict, stop: dict) -> None:
    for key in ("lat", "lon"):
        if stop.get(key) is not None:
            stop_data[key] = stop[key]


def parse_owntracks_review_line(line: str) -> tuple[str, dict]:
    segments = [segment.strip() for segment in line.split("|")]
    first = segments[0].split(maxsplit=1)
    if len(first) < 2:
        raise ValueError(f"Missing name: {line}")
    stop_ref, name = first[0], first[1].strip()
    update: dict[str, object] = {"name": name}
    for segment in segments[1:]:
        key, sep, value = segment.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "tags":
            update["tags"] = [tag for tag in re.split(r"[\s,]+", value) if tag]
        elif key == "note":
            update["note"] = value
    return stop_ref, update


async def owntracks_names_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    lines = [line.strip() for line in message.text.splitlines() if line.strip()]
    if not lines:
        return
    first_parts = lines[0].split(maxsplit=1)
    date_text = remembered_owntracks_date(update, context) or "today"
    inline_pairs: list[str] = []
    if len(first_parts) > 1:
        tail = first_parts[1].strip()
        if OWNTRACKS_DATE_RE.fullmatch(tail):
            date_text = tail
        else:
            inline_pairs.append(tail)
    pair_lines = inline_pairs + lines[1:]
    if not pair_lines:
        await message.reply_text(
            f"Usage:\n/otb {OWNTRACKS_DATE_USAGE}\n"
            "s1 Place name | tags: tag1 tag2 | note: what happened"
        )
        return
    try:
        plan, _digest, _path = await asyncio.to_thread(generate_owntracks_digest, date_text)
    except Exception as exc:
        await message.reply_text(f"Could not load OwnTracks stops: {exc}")
        return
    stops_by_ref = {
        ref: stop
        for stop in plan["candidate_stops"]
        for ref in (stop["id"], stop.get("alias"))
        if ref
    }
    updates: list[tuple[dict, dict]] = []
    errors: list[str] = []
    for line in pair_lines:
        try:
            stop_ref, update = parse_owntracks_review_line(line)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        stop = stops_by_ref.get(stop_ref)
        if not stop:
            errors.append(f"Unknown stop: {stop_ref}")
            continue
        if not update.get("name"):
            errors.append(f"Missing name: {stop_ref}")
            continue
        updates.append((stop, update))
    if not updates:
        await message.reply_text("No reviews saved. " + "; ".join(errors[:5]))
        return
    tags_path = config.owntracks_user_tags_path
    data = load_owntracks_user_tags(tags_path)
    stops = data.setdefault(plan["date"], {}).setdefault("stops", {})
    for stop, update in updates:
        stop_id = stop["id"]
        stop_data = stops.setdefault(stop_id, {})
        annotate_saved_stop(stop_data, stop)
        if update.get("name"):
            stop_data["name"] = update["name"]
        if "tags" in update:
            stop_data["tags"] = update["tags"]
        if "note" in update:
            stop_data["note"] = update["note"]
    save_owntracks_user_tags(tags_path, data)
    remember_owntracks_date(update, context, plan["date"])
    suffix = f"\nSkipped: {'; '.join(errors[:5])}" if errors else ""
    await message.reply_text(f"Saved {len(updates)} OwnTracks stop reviews for {plan['date']}.{suffix}")


async def owntracks_tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    date_text, args, error = owntracks_date_and_args(update, context, min_tail=2)
    if error:
        await update.effective_message.reply_text(error)
        return
    if len(args) < 2 or not date_text:
        await update.effective_message.reply_text(f"Usage: /ott s1 tag1 tag2 or /ott {OWNTRACKS_DATE_USAGE} s1 tag1 tag2")
        return
    stop_ref = args[0]
    tags = args[1:]
    try:
        stop, target_date = await asyncio.to_thread(resolve_owntracks_stop, date_text, stop_ref)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    tags_path = config.owntracks_user_tags_path
    data = load_owntracks_user_tags(tags_path)
    stop_id = stop["id"]
    stop_data = data.setdefault(target_date, {}).setdefault("stops", {}).setdefault(stop_id, {})
    annotate_saved_stop(stop_data, stop)
    existing = stop_data.setdefault("tags", [])
    for tag in tags:
        if tag not in existing:
            existing.append(tag)
    save_owntracks_user_tags(tags_path, data)
    remember_owntracks_date(update, context, target_date)
    await update.effective_message.reply_text(f"Saved tags for {target_date} {stop_id}: {', '.join(existing)}")


async def owntracks_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    date_text, args, error = owntracks_date_and_args(update, context, min_tail=2)
    if error:
        await update.effective_message.reply_text(error)
        return
    if len(args) < 2 or not date_text:
        await update.effective_message.reply_text(f"Usage: /otn s1 place name or /otn {OWNTRACKS_DATE_USAGE} s1 place name")
        return
    stop_ref = args[0]
    name = " ".join(args[1:])
    try:
        stop, target_date = await asyncio.to_thread(resolve_owntracks_stop, date_text, stop_ref)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    tags_path = config.owntracks_user_tags_path
    data = load_owntracks_user_tags(tags_path)
    stop_id = stop["id"]
    stop_data = data.setdefault(target_date, {}).setdefault("stops", {}).setdefault(stop_id, {})
    annotate_saved_stop(stop_data, stop)
    stop_data["name"] = name
    save_owntracks_user_tags(tags_path, data)
    remember_owntracks_date(update, context, target_date)
    await update.effective_message.reply_text(f"Saved name for {target_date} {stop_id}: {name}")


async def owntracks_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    date_text, args, error = owntracks_date_and_args(update, context, min_tail=2)
    if error:
        await update.effective_message.reply_text(error)
        return
    if len(args) < 2 or not date_text:
        await update.effective_message.reply_text(f"Usage: /oto s1 what happened or /oto {OWNTRACKS_DATE_USAGE} s1 what happened")
        return
    stop_ref = args[0]
    note = " ".join(args[1:])
    try:
        stop, target_date = await asyncio.to_thread(resolve_owntracks_stop, date_text, stop_ref)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    tags_path = config.owntracks_user_tags_path
    data = load_owntracks_user_tags(tags_path)
    stop_id = stop["id"]
    stop_data = data.setdefault(target_date, {}).setdefault("stops", {}).setdefault(stop_id, {})
    annotate_saved_stop(stop_data, stop)
    stop_data["note"] = note
    save_owntracks_user_tags(tags_path, data)
    remember_owntracks_date(update, context, target_date)
    await update.effective_message.reply_text(f"Saved note for {target_date} {stop_id}.")


async def owntracks_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not await owntracks_guarded(update, config):
        return
    await update.effective_message.reply_text(
        "OwnTracks commands:\n"
        f"/otd [{OWNTRACKS_DATE_USAGE}]\n"
        f"/otm [{OWNTRACKS_DATE_USAGE}]\n"
        f"/otb {OWNTRACKS_DATE_USAGE} then lines like: s1 Place | tags: tag1 tag2 | note: text\n"
        "/ott s1 tag1 tag2\n"
        "/otn s1 place name\n"
        "/oto s1 what happened\n"
        "Run /otd first; s1/s2 aliases come from the latest digest."
    )


async def post_init(application: Application) -> None:
    config: Config = application.bot_data["config"]
    commands = [BotCommand(name, description) for name, description in COMMAND_SHORTCUTS]
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
    application.add_handler(CommandHandler(["cmd", "commands", "help"], command_shortcuts_command))
    application.add_handler(CommandHandler(["cxr", "codex_start"], codex_start))
    application.add_handler(CommandHandler(["cxs", "codex_status"], codex_status))
    application.add_handler(CommandHandler(["cxq", "codex_stop"], codex_stop))
    application.add_handler(CommandHandler(["memq", "memory_query"], memory_query_command))
    application.add_handler(CommandHandler(["oth", "owntracks", "owntracks_help", "ot_help"], owntracks_help_command))
    application.add_handler(CommandHandler(["otd", "ot", "owntracks_digest", "ot_digest"], owntracks_digest_command))
    application.add_handler(CommandHandler(["otm", "ot_map", "owntracks_map"], owntracks_map_command))
    application.add_handler(CommandHandler(["otb", "ot_names", "owntracks_names"], owntracks_names_command))
    application.add_handler(CommandHandler(["ott", "tag", "owntracks_tag", "ot_tag"], owntracks_tag_command))
    application.add_handler(CommandHandler(["otn", "name", "owntracks_name", "ot_name"], owntracks_name_command))
    application.add_handler(CommandHandler(["oto", "note", "owntracks_note", "ot_note"], owntracks_note_command))
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
