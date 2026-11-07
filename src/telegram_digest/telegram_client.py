from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from .config import Settings
from .models import ChatInfo, DigestMessage


def create_client(settings: Settings) -> TelegramClient:
    return TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)


async def list_group_dialogs(client: TelegramClient) -> list[ChatInfo]:
    chats: list[ChatInfo] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Channel):
            kind = "频道" if entity.broadcast else "超级群组"
        elif isinstance(entity, Chat):
            kind = "群组"
        else:
            continue
        chats.append(ChatInfo(peer_id=int(dialog.id), title=dialog.name or "未命名会话", kind=kind))
    return chats


async def collect_messages(
    client: TelegramClient,
    peer_id: int,
    last_message_id: int,
    initial_lookback_hours: int,
    limit: int,
) -> list[DigestMessage]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=initial_lookback_hours)
    kwargs: dict[str, Any] = {"entity": peer_id, "limit": limit}
    if last_message_id > 0:
        kwargs["min_id"] = last_message_id
        kwargs["reverse"] = True

    collected: list[DigestMessage] = []
    async for message in client.iter_messages(**kwargs):
        if last_message_id == 0 and message.date and message.date < cutoff:
            continue
        text = (message.raw_text or "").strip()
        if not text:
            if message.media:
                text = "[媒体消息]"
            elif message.action:
                text = "[服务消息]"
            else:
                continue
        sender = await _sender_name(message)
        collected.append(
            DigestMessage(
                message_id=int(message.id),
                date=message.date,
                sender=sender,
                text=text,
            )
        )
    collected.sort(key=lambda item: item.message_id)
    return collected


async def _sender_name(message: Any) -> str:
    sender = await message.get_sender()
    if sender is None:
        return "未知用户"
    title = getattr(sender, "title", None)
    if title:
        return str(title)
    first_name = getattr(sender, "first_name", "") or ""
    last_name = getattr(sender, "last_name", "") or ""
    username = getattr(sender, "username", "") or ""
    full_name = f"{first_name} {last_name}".strip()
    return full_name or (f"@{username}" if username else str(getattr(sender, "id", "未知用户")))

