from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from telethon.errors import FloodWaitError, RPCError

from .config import ensure_runtime_files, load_settings, save_selected_chats
from .jsonl_writer import write_chat_messages
from .paths import runtime_dir
from .state_store import StateStore
from .telegram_client import collect_messages, create_client, list_group_dialogs


PROJECT_DIR = runtime_dir()


@dataclass
class DigestRunResult:
    paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="个人 Telegram 群聊与频道消息导出工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="创建默认配置")
    subparsers.add_parser("login", help="登录并验证 Telegram 账号")
    subparsers.add_parser("chats", help="列出可访问的群聊和频道")
    subparsers.add_parser("select", help="交互式选择需要导出的会话")
    subparsers.add_parser("digest", help="增量读取并导出 JSONL")
    subparsers.add_parser("menu", help="打开交互式菜单")
    return parser


async def command_login() -> None:
    settings = load_settings(PROJECT_DIR)
    client = create_client(settings)
    async with client:
        me = await client.get_me()
        print(f"登录成功：{me.first_name or ''}，用户ID：{me.id}")


async def command_chats(select_mode: bool = False) -> None:
    settings = load_settings(PROJECT_DIR)
    client = create_client(settings)
    async with client:
        chats = await list_group_dialogs(client)
    if not chats:
        print("没有找到可访问的群聊或频道。")
        return
    for index, chat in enumerate(chats, start=1):
        print(f"[{index:>3}] {chat.kind:<4} {chat.title}  ID={chat.peer_id}")
    if not select_mode:
        return
    raw = input("\n请输入需要关注的序号，多个序号用逗号分隔：").strip()
    indexes = _parse_indexes(raw, len(chats))
    selected = [
        {"peer_id": chats[index - 1].peer_id, "title": chats[index - 1].title, "kind": chats[index - 1].kind}
        for index in indexes
    ]
    save_selected_chats(PROJECT_DIR, selected)
    print(f"已保存 {len(selected)} 个会话。")


def _parse_indexes(raw: str, maximum: int) -> list[int]:
    try:
        values = sorted({int(value.strip()) for value in raw.replace("，", ",").split(",") if value.strip()})
    except ValueError as exc:
        raise ValueError("序号格式错误，请使用逗号分隔数字，例如：1,3,5") from exc
    if not values or values[0] < 1 or values[-1] > maximum:
        raise ValueError(f"序号必须在 1 到 {maximum} 之间。")
    return values


async def command_digest(reporter=print) -> DigestRunResult:
    settings = load_settings(PROJECT_DIR)
    if not settings.selected_chats:
        raise ValueError("尚未选择会话，请先运行 select。")
    state_store = StateStore(settings.state_path)
    state = state_store.load()
    result = DigestRunResult()
    client = create_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("账号尚未登录，请先完成登录。")
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

async def dispatch(command: str) -> None:
    if command == "login":
        await command_login()
    elif command == "chats":
        await command_chats()
    elif command == "select":
        await command_chats(select_mode=True)
    elif command == "digest":
        result = await command_digest()
        if result.warnings and not result.paths:
            raise RuntimeError("；".join(result.warnings))


def interactive_menu() -> None:
    actions = {
        "2": "login",
        "3": "chats",
        "4": "select",
        "5": "digest",
    }
    while True:
        print(
            "\n========================================\n"
            "       Telegram 个人消息导出工具\n"
            "========================================\n"
            "1. 编辑 API 配置\n"
            "2. 登录或检查登录状态\n"
            "3. 查看群聊和频道\n"
            "4. 选择需要关注的会话\n"
            "5. 导出增量消息\n"
            "6. 打开数据目录\n"
            "0. 退出\n"
        )
        choice = input("请输入序号：").strip()
        if choice == "0":
            return
        if choice == "1":
            ensure_runtime_files(PROJECT_DIR)
            os.startfile(PROJECT_DIR / ".env")
        elif choice == "6":
            ensure_runtime_files(PROJECT_DIR)
            os.startfile(PROJECT_DIR / "output")
        elif choice in actions:
            try:
                asyncio.run(dispatch(actions[choice]))
            except FloodWaitError as exc:
                print(f"Telegram 要求等待 {exc.seconds} 秒后再试。")
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"错误：{exc}")
        else:
            print("无效序号，请重新输入。")
        input("\n按回车键返回菜单……")


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init":
        ensure_runtime_files(PROJECT_DIR)
        print(f"默认配置已创建：{PROJECT_DIR / 'data' / 'config.json'}")
        return
    if args.command == "menu":
        interactive_menu()
        return
    try:
        asyncio.run(dispatch(args.command))
    except FloodWaitError as exc:
        print(f"Telegram 要求等待 {exc.seconds} 秒后再试。", file=sys.stderr)
        raise SystemExit(2) from exc
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

