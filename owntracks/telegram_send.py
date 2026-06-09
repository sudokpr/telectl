from __future__ import annotations

import json
import urllib.parse
import urllib.request


def send_telegram_message(bot_token: str, chat_id: str, text: str, topic_id: int | None = None) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = split_message(text)
    for chunk in chunks:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": False,
        }
        if topic_id is not None:
            payload["message_thread_id"] = topic_id
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram send failed: {body}")


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        remaining = line
        while len(remaining) + 1 > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        line_len = len(remaining) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(remaining)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks
