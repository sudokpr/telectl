from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import claim_preceding_caption, message_has_pdf, wait_for_caption_pair


def message(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        chat_id=-1001,
        message_thread_id=145,
        from_user=SimpleNamespace(id=42),
    )


def test_consecutive_text_and_image_are_paired() -> None:
    async def scenario():
        application = SimpleNamespace(bot_data={})
        text_message = message(1270)
        image_message = message(1271)
        waiting = asyncio.create_task(
            wait_for_caption_pair(application, text_message, "Coriander is misspelt", 1)
        )
        await asyncio.sleep(0)

        claimed = await claim_preceding_caption(application, image_message, wait_seconds=0)

        assert claimed == ("Coriander is misspelt", 1270)
        assert await waiting is True
        assert application.bot_data["pending_image_captions"] == {}

    asyncio.run(scenario())


def test_nonconsecutive_image_does_not_claim_text() -> None:
    async def scenario():
        application = SimpleNamespace(bot_data={})
        waiting = asyncio.create_task(wait_for_caption_pair(application, message(1270), "Standalone note", 0.01))
        await asyncio.sleep(0)

        claimed = await claim_preceding_caption(application, message(1272), wait_seconds=0)

        assert claimed is None
        assert await waiting is False

    asyncio.run(scenario())


def test_message_has_pdf_recognizes_replied_pdf() -> None:
    replied = SimpleNamespace(document=SimpleNamespace(mime_type="application/pdf"))
    image = SimpleNamespace(document=SimpleNamespace(mime_type="image/jpeg"))

    assert message_has_pdf(replied)
    assert not message_has_pdf(image)
