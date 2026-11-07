from __future__ import annotations

import json
import inspect
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from telegram_digest.config import (
    DEFAULT_CONFIG,
    ensure_runtime_files,
    read_credentials,
    save_credentials,
    save_selected_chats,
)
from telegram_digest.jsonl_writer import safe_chat_name, write_chat_messages
from telegram_digest.models import DigestMessage
from telegram_digest.state_store import StateStore
from telegram_digest.telegram_client import collect_messages
import telegram_digest.telegram_client as telegram_client_module
import telegram_digest.cli as cli_module
from telegram_digest.phone import normalize_phone
from telegram_digest.paths import runtime_dir


class CoreTests(unittest.TestCase):
    def test_default_config_has_no_model_provider(self) -> None:
        self.assertNotIn("summarizer", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["chat_groups"], [])

    def test_state_is_kept_per_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.update_many({-1001: 5, -1002: 9})
            store.update_many({-1001: 6})
            self.assertEqual(store.load(), {"-1001": 6, "-1002": 9})

    def test_selected_chats_preserve_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ensure_runtime_files(root)
            save_selected_chats(root, [{"peer_id": -1001, "title": "测试群", "kind": "群组"}])
            config = json.loads((root / "data" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["initial_lookback_hours"], 24)
            self.assertEqual(config["selected_chats"][0]["peer_id"], -1001)

    def test_legacy_model_config_is_removed_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            legacy = {
                **DEFAULT_CONFIG,
                "summarizer": {"provider": "removed_model", "model": "old"},
                "send_to_saved_messages": True,
            }
            (root / "data" / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
            ensure_runtime_files(root)
            config = json.loads((root / "data" / "config.json").read_text(encoding="utf-8"))
            self.assertNotIn("summarizer", config)
            self.assertNotIn("send_to_saved_messages", config)

    def test_credentials_are_saved_without_logging_or_extra_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_credentials(root, "123456", "0123456789abcdef0123456789abcdef")
            self.assertEqual(
                read_credentials(root),
                ("123456", "0123456789abcdef0123456789abcdef"),
            )
            content = (root / ".env").read_text(encoding="utf-8")
            self.assertNotIn("PHONE", content)
            self.assertNotIn("PASSWORD", content)

    def test_phone_country_code_and_number_are_combined(self) -> None:
        self.assertEqual(normalize_phone("+86", "138 0013 8000"), "+8613800138000")
        self.assertEqual(normalize_phone("1", "202-555-0100"), "+12025550100")

    def test_invalid_phone_number_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_phone("+86", "")
        with self.assertRaises(ValueError):
            normalize_phone("+86", "138abc")

    def test_frozen_runtime_uses_executable_directory(self) -> None:
        fake_executable = str(Path("C:/Portable/TelegramDigest/TelegramDigest.exe"))
        with patch("telegram_digest.paths.sys.frozen", True, create=True), patch(
            "telegram_digest.paths.sys.executable", fake_executable
        ):
            self.assertEqual(runtime_dir(), Path(fake_executable).resolve().parent)

    def test_runtime_directory_can_be_overridden_for_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "telegram_digest.paths.os.environ", {"TELEGRAM_DIGEST_RUNTIME_DIR": directory}
        ):
            self.assertEqual(runtime_dir(), Path(directory).resolve())

    def test_jsonl_record_is_self_contained_and_preserves_text(self) -> None:
        message = DigestMessage(
            7,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "张三",
            '作业明天截止\n包含“引号”和\\路径',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_chat_messages(Path(directory), "测试群", -1001, "群组", [message])
            self.assertTrue(path.exists())
            self.assertEqual(path.suffix, ".jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["schema_version"], 1)
            self.assertEqual(record["chat_id"], -1001)
            self.assertEqual(record["chat_title"], "测试群")
            self.assertEqual(record["chat_kind"], "群组")
            self.assertEqual(record["message_id"], 7)
            self.assertEqual(record["sender"], "张三")
            self.assertEqual(record["text"], message.text)
            parsed_timestamp = datetime.fromisoformat(record["timestamp"])
            self.assertIsNotNone(parsed_timestamp.utcoffset())

    def test_jsonl_filenames_do_not_collide(self) -> None:
        message = DigestMessage(1, datetime(2026, 1, 1, tzinfo=timezone.utc), "用户", "消息")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            moment = datetime(2026, 1, 1, 8, 0, 0, 1)
            first = write_chat_messages(root, "同名群", -1001, "群组", [message], moment)
            second = write_chat_messages(root, "同名群", -1001, "群组", [message], moment)
            self.assertNotEqual(first.name, second.name)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_jsonl_publish_failure_leaves_no_partial_file(self) -> None:
        message = DigestMessage(1, datetime(2026, 1, 1, tzinfo=timezone.utc), "用户", "消息")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("telegram_digest.jsonl_writer.os.link", side_effect=OSError("磁盘写入失败")):
                with self.assertRaisesRegex(OSError, "磁盘写入失败"):
                    write_chat_messages(root, "测试群", -1001, "群组", [message])
            self.assertEqual(list(root.iterdir()), [])

    def test_chat_filename_is_windows_safe_and_keeps_peer_id(self) -> None:
        self.assertEqual(safe_chat_name(' 课程/作业:*?"<>|. '), "课程_作业")
        message = DigestMessage(1, datetime(2026, 1, 1, tzinfo=timezone.utc), "用户", "消息")
        with tempfile.TemporaryDirectory() as directory:
            path = write_chat_messages(
                Path(directory), '课程/作业:*?"<>|', -10088, "群组", [message]
            )
            self.assertIn("-10088", path.name)
            self.assertNotRegex(path.name, r'[<>:"/\\|?*]')

    def test_client_code_never_sends_read_acknowledgement(self) -> None:
        source = inspect.getsource(telegram_client_module)
        forbidden = "send_" + "read_acknowledge"
        self.assertNotIn(forbidden, source)


class _FakeMessage:
    def __init__(self, message_id: int, text: str):
        self.id = message_id
        self.raw_text = text
        self.date = datetime.now(timezone.utc)
        self.media = None
        self.action = None

    async def get_sender(self):
        return type("Sender", (), {"first_name": "测试", "last_name": "用户", "username": None})()


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.last_kwargs = None

    async def iter_messages(self, **kwargs):
        self.last_kwargs = kwargs
        for message in self.messages:
            yield message


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_run_requests_latest_messages(self) -> None:
        client = _FakeClient([_FakeMessage(9, "通知")])
        messages = await collect_messages(client, -1001, 0, 24, 500)
        self.assertEqual(messages[0].message_id, 9)
        self.assertNotIn("reverse", client.last_kwargs)
        self.assertNotIn("min_id", client.last_kwargs)

    async def test_incremental_run_uses_per_chat_min_id(self) -> None:
        client = _FakeClient([_FakeMessage(10, "新消息")])
        await collect_messages(client, -1001, 9, 24, 500)
        self.assertEqual(client.last_kwargs["min_id"], 9)
        self.assertTrue(client.last_kwargs["reverse"])


class _DigestClient:
    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        return True


class DigestCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_dispatch_fails_when_no_chat_was_completed(self) -> None:
        failed = cli_module.DigestRunResult(warnings=["会话读取失败"])
        with patch.object(cli_module, "command_digest", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "会话读取失败"):
                await cli_module.dispatch("digest")

    async def test_each_chat_generates_an_independent_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ensure_runtime_files(root)
            save_credentials(root, "123456", "0123456789abcdef0123456789abcdef")
            save_selected_chats(
                root,
                [
                    {"peer_id": -1001, "title": "课程群", "kind": "群组"},
                    {"peer_id": -1002, "title": "通知群", "kind": "频道"},
                ],
            )
            first = [DigestMessage(11, datetime(2026, 1, 1, tzinfo=timezone.utc), "甲", "课程作业")]
            second = [DigestMessage(22, datetime(2026, 1, 1, tzinfo=timezone.utc), "乙", "考试通知")]
            with patch.object(cli_module, "PROJECT_DIR", root), patch.object(
                cli_module, "create_client", return_value=_DigestClient()
            ), patch.object(
                cli_module, "collect_messages", side_effect=[first, second]
            ):
                result = await cli_module.command_digest(reporter=lambda _message: None)

            self.assertEqual(len(result.paths), 2)
            self.assertEqual(result.warnings, [])
            self.assertTrue(all(path.suffix == ".jsonl" for path in result.paths))
            records = [json.loads(path.read_text(encoding="utf-8").strip()) for path in result.paths]
            self.assertEqual(records[0]["text"], "课程作业")
            self.assertEqual(records[0]["chat_id"], -1001)
            self.assertEqual(records[1]["text"], "考试通知")
            self.assertEqual(records[1]["chat_id"], -1002)
            self.assertEqual(StateStore(root / "data" / "state.json").load(), {"-1001": 11, "-1002": 22})

    async def test_later_chat_failure_keeps_earlier_file_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ensure_runtime_files(root)
            save_credentials(root, "123456", "0123456789abcdef0123456789abcdef")
            save_selected_chats(root, [
                {"peer_id": -1001, "title": "成功群", "kind": "群组"},
                {"peer_id": -1002, "title": "失败群", "kind": "群组"},
            ])
            first = [DigestMessage(11, datetime(2026, 1, 1, tzinfo=timezone.utc), "甲", "已保存消息")]
            with patch.object(cli_module, "PROJECT_DIR", root), patch.object(
                cli_module, "create_client", return_value=_DigestClient()
            ), patch.object(
                cli_module, "collect_messages", side_effect=[first, OSError("网络中断")]
            ):
                result = await cli_module.command_digest(reporter=lambda _message: None)

            self.assertEqual(len(result.paths), 1)
            self.assertEqual(len(result.warnings), 1)
            self.assertEqual(StateStore(root / "data" / "state.json").load(), {"-1001": 11})

if __name__ == "__main__":
    unittest.main()

