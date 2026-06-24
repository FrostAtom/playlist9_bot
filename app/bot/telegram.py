"""Thin wrappers around aiogram calls that tolerate the usual transient errors
(message too old, already deleted, "not modified") so callers don't repeat the
try/except. Shared by the router and the delivery pipeline.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message


async def safe_edit(message: Message, text: str, **kwargs) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        pass


async def safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass  # too old / already gone / not deletable


async def safe_inline_edit(bot: Bot, inline_message_id: str, text: str) -> None:
    try:
        await bot.edit_message_text(text, inline_message_id=inline_message_id)
    except TelegramBadRequest:
        pass


def delete_after(message: Message, delay: float) -> None:
    """Schedule a fire-and-forget deletion of ``message`` after ``delay`` seconds."""

    async def _later() -> None:
        await asyncio.sleep(delay)
        await safe_delete(message)

    asyncio.create_task(_later())


async def answer_ephemeral(
    target: Message, text: str, delay: float, **kwargs
) -> Message:
    """Send a transient status/notice message and schedule its deletion after
    ``delay`` seconds.

    This is the single place every non-music message gets its lifetime: status
    messages (searching/downloading/loading), prompts and error notices all flow
    through here. On the happy path the delivery pipeline deletes the status as
    soon as the audio is sent; otherwise — including any download error — the
    scheduled deletion removes it after ``delay``, exactly like a search result.
    """
    msg = await target.answer(text, **kwargs)
    delete_after(msg, delay)
    return msg
