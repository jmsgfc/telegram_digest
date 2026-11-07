from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import telegram_digest.web as web
from telegram_digest.config import ensure_runtime_files, save_credentials
from telegram_digest.digest import DigestRunResult
from telegram_digest.models import ChatInfo


class WebTests(unittest.TestCase):
    write_headers = {"X-Telegram-Digest": "local-web-ui"}

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary_directory.name)
        ensure_runtime_files(self.runtime)
        web.jobs.clear()
        web.pending_login.clear()
        web.chat_cache.clear()
        self.runtime_patch = patch.object(web, "RUNTIME_DIR", self.runtime)
        self.runtime_patch.start()
        self.client = TestClient(web.app)

    def tearDown(self) -> None:
        self.runtime_patch.stop()
        self.temporary_directory.cleanup()

    def test_health_and_home_are_available(self) -> None:
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("把喧闹的消息", home.text)

        styles = (web.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("animation: view-in", styles)
        self.assertIn('input.className = "chat-checkbox"', script)
        self.assertIn("downloadSelectedDigests", script)
        self.assertIn("function digestPeriod", script)
        self.assertIn('id="digestSearch"', home.text)
        self.assertIn("下载 JSONL", home.text)
        self.assertIn('id="openPrompt"', home.text)
        self.assertIn('id="summaryPrompt"', home.text)
        self.assertIn("生成一张中文总结图片", home.text)
        self.assertIn('$("#copyPrompt").addEventListener("click", copySummaryPrompt)', script)
        self.assertNotIn('id="heroDigest"', home.text)
        self.assertNotIn("data-view-target", home.text)
        self.assertNotIn('id="refreshHistory"', home.text)

    def test_status_never_returns_api_hash(self) -> None:
        secret_hash = "0123456789abcdef0123456789abcdef"
        save_credentials(self.runtime, "123456", secret_hash)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])
        self.assertNotIn(secret_hash, response.text)
        self.assertNotIn("api_hash", response.text)

    def test_settings_only_accept_local_reading_options(self) -> None:
        response = self.client.put(
            "/api/settings",
            headers=self.write_headers,
            json={
                "initial_lookback_hours": 24,
                "max_messages_per_chat": 500,
            },
        )
        self.assertEqual(response.status_code, 200)
        status = self.client.get("/api/status").json()
        self.assertNotIn("provider", status["settings"])
        self.assertNotIn("send_to_saved_messages", status["settings"])

    def test_cross_site_write_without_local_header_is_rejected(self) -> None:
        response = self.client.post(
            "/api/digests",
            headers={"Origin": "https://evil.example", "Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(web.jobs, {})

    def test_data_file_name_rejects_path_traversal(self) -> None:
        response = self.client.get("/api/digests/not-a-data-file.jsonl")
        self.assertEqual(response.status_code, 404)

    def test_per_chat_jsonl_with_unicode_title_can_be_read(self) -> None:
        name = "messages-20261107-120000-123456--1001-课程群.jsonl"
        path = self.runtime / "output" / name
        content = '{"chat_id":-1001,"text":"课程群消息"}\n'
        path.write_text(content, encoding="utf-8")
        response = self.client.get(f"/api/digests/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", response.headers["content-type"])
        self.assertEqual(response.text, content)

    def test_selected_jsonl_files_can_be_downloaded_as_zip(self) -> None:
        first = self.runtime / "output" / "messages-20261107-120000-123456--1001-课程群.jsonl"
        second = self.runtime / "output" / "messages-20261107-120001-123456--1002-通知频道.jsonl"
        first.write_bytes('{"text":"课程群消息"}\n'.encode("utf-8"))
        second.write_bytes('{"text":"通知频道消息"}\n'.encode("utf-8"))

        response = self.client.post(
            "/api/digests/archive",
            headers=self.write_headers,
            json={"filenames": [first.name, second.name, first.name]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("attachment", response.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), [first.name, second.name])
            self.assertEqual(archive.read(first.name).decode("utf-8"), '{"text":"课程群消息"}\n')
            self.assertEqual(archive.read(second.name).decode("utf-8"), '{"text":"通知频道消息"}\n')

    def test_history_only_lists_jsonl_message_files(self) -> None:
        output = self.runtime / "output"
        jsonl = output / "messages-20261107-120000-123456--1001-课程群.jsonl"
        legacy = output / "digest-20261107-课程群.md"
        jsonl.write_bytes(b'{"text":"new"}\n')
        legacy.write_text("# old", encoding="utf-8")

        response = self.client.get("/api/digests")

        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.json()["digests"]]
        self.assertEqual(names, [jsonl.name])
        self.assertNotIn(legacy.name, names)

    def test_digest_archive_rejects_invalid_or_missing_files(self) -> None:
        missing = self.client.post(
            "/api/digests/archive",
            headers=self.write_headers,
            json={"filenames": ["messages-missing.jsonl"]},
        )
        self.assertEqual(missing.status_code, 404)

        traversal = self.client.post(
            "/api/digests/archive",
            headers=self.write_headers,
            json={"filenames": ["../messages-secret.jsonl"]},
        )
        self.assertEqual(traversal.status_code, 404)

        empty = self.client.post(
            "/api/digests/archive",
            headers=self.write_headers,
            json={"filenames": []},
        )
        self.assertEqual(empty.status_code, 422)

        too_many = self.client.post(
            "/api/digests/archive",
            headers=self.write_headers,
            json={"filenames": [f"messages-{index}.jsonl" for index in range(101)]},
        )
        self.assertEqual(too_many.status_code, 422)

    def test_digest_archive_requires_local_header(self) -> None:
        response = self.client.post(
            "/api/digests/archive",
            json={"filenames": ["messages-missing.jsonl"]},
        )
        self.assertEqual(response.status_code, 403)

    def test_chat_group_can_be_saved_updated_and_deleted(self) -> None:
        save_credentials(self.runtime, "123456", "0123456789abcdef0123456789abcdef")
        web.chat_cache.update(
            {
                -1001: ChatInfo(peer_id=-1001, title="课程群", kind="群组"),
                -1002: ChatInfo(peer_id=-1002, title="通知频道", kind="频道"),
            }
        )

        created = self.client.put(
            "/api/chat-groups",
            headers=self.write_headers,
            json={"name": "学习", "peer_ids": [-1001, -1002, -1001]},
        )
        self.assertEqual(created.status_code, 200)
        group = created.json()["groups"][0]
        self.assertEqual(group["name"], "学习")
        self.assertEqual(group["peer_ids"], [-1001, -1002])

        updated = self.client.put(
            "/api/chat-groups",
            headers=self.write_headers,
            json={"name": "学习", "peer_ids": [-1002]},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(len(updated.json()["groups"]), 1)
        self.assertEqual(updated.json()["groups"][0]["peer_ids"], [-1002])

        loaded = self.client.get("/api/chat-groups")
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()["groups"][0]["name"], "学习")

        deleted = self.client.delete(
            f"/api/chat-groups/{group['id']}",
            headers=self.write_headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["groups"], [])

    def test_chat_group_rejects_unknown_chat(self) -> None:
        save_credentials(self.runtime, "123456", "0123456789abcdef0123456789abcdef")
        web.chat_cache[-1001] = ChatInfo(peer_id=-1001, title="课程群", kind="群组")
        response = self.client.put(
            "/api/chat-groups",
            headers=self.write_headers,
            json={"name": "学习", "peer_ids": [-9999]},
        )
        self.assertEqual(response.status_code, 422)

    def test_chat_group_write_requires_local_header(self) -> None:
        response = self.client.put(
            "/api/chat-groups",
            json={"name": "学习", "peer_ids": [-1001]},
        )
        self.assertEqual(response.status_code, 403)


class WebJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_job_returns_all_generated_files(self) -> None:
        job_id = "test-job"
        web.jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "messages": [],
            "results": [],
            "error": None,
            "created_at": 0,
        }
        paths = [Path("messages-a.jsonl"), Path("messages-b.jsonl")]
        result = DigestRunResult(paths=paths)
        with patch.object(web, "export_selected_messages", return_value=result):
            await web._run_digest_job(job_id)
        self.assertEqual(web.jobs[job_id]["status"], "succeeded")
        self.assertEqual(web.jobs[job_id]["results"], ["messages-a.jsonl", "messages-b.jsonl"])

    async def test_partial_job_preserves_successful_files(self) -> None:
        job_id = "partial-job"
        web.jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "messages": [],
            "results": [],
            "warnings": [],
            "error": None,
            "created_at": 0,
        }
        result = DigestRunResult(paths=[Path("messages-a.jsonl")], warnings=["第二个会话读取失败"])
        with patch.object(web, "export_selected_messages", return_value=result):
            await web._run_digest_job(job_id)
        self.assertEqual(web.jobs[job_id]["status"], "partial")
        self.assertEqual(web.jobs[job_id]["results"], ["messages-a.jsonl"])


if __name__ == "__main__":
    unittest.main()
