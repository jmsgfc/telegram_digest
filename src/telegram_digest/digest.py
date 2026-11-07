from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from telethon.errors import FloodWaitError, RPCError

from .config import load_settings
from .jsonl_writer import write_chat_messages
from .state_store import StateStore
from .telegram_client import collect_messages, create_client


@dataclass
class DigestRunResult:
    paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def export_selected_messages(
    runtime_root: Path,
    reporter: Callable[[str], None] = print,
) -> DigestRunResult:
    settings = load_settings(runtime_root)
    if not settings.selected_chats:
        raise ValueError("尚未选择会话，请先在网页中保存关注会话。")

    state_store = StateStore(settings.state_path)
    state = state_store.load()
    result = DigestRunResult()
    client = create_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("账号尚未登录，请先在网页中完成登录。")

        for chat in settings.selected_chats:
            peer_id = int(chat["peer_id"])
            title = str(chat.get("title") or peer_id)
            kind = str(chat.get("kind") or "")
            last_id = int(state.get(str(peer_id), 0))
            try:
                messages = await collect_messages(
                    client,
                    peer_id,
                    last_id,
                    settings.initial_lookback_hours,
                    settings.max_messages_per_chat,
                )
            except FloodWaitError as exc:
                warning = f"{title}：Telegram 限流，请等待 {exc.seconds} 秒后重试"
                result.warnings.append(warning)
                reporter(warning)
                break
            except (RPCError, ValueError, RuntimeError, OSError) as exc:
                warning = f"{title}：读取失败（{exc}）"
                result.warnings.append(warning)
                reporter(warning)
                break

            if not messages:
                reporter(f"{title}：没有新消息")
                continue

            try:
                path = write_chat_messages(settings.output_dir, title, peer_id, kind, messages)
                result.paths.append(path)
                state_store.update_many({peer_id: messages[-1].message_id})
            except (ValueError, RuntimeError, OSError) as exc:
                warning = f"{title}：文件或进度保存失败（{exc}）"
                result.warnings.append(warning)
                reporter(warning)
                break
            reporter(f"{title}：已导出 {len(messages)} 条新消息，生成 {path.name}")

        if not result.paths and not result.warnings:
            reporter("所有选定会话均无新消息，本次未导出文件。")
        elif result.paths:
            reporter(f"消息导出完成：共 {len(result.paths)} 份 JSONL 文件")
        return result
    finally:
        await client.disconnect()
