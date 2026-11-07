from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from itertools import count
from pathlib import Path

from .models import DigestMessage


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_chat_name(title: str, limit: int = 60) -> str:
    cleaned = INVALID_FILENAME.sub("_", title)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")[:limit].rstrip(" ._")
    return cleaned or "chat"


def write_chat_messages(
    output_dir: Path,
    title: str,
    peer_id: int,
    kind: str,
    messages: list[DigestMessage],
    now: datetime | None = None,
) -> Path:
    timestamp = now or datetime.now().astimezone()
    records = [
        {
            "schema_version": 1,
            "chat_id": peer_id,
            "chat_title": title,
            "chat_kind": kind,
            "message_id": message.message_id,
            "timestamp": message.date.astimezone().isoformat(timespec="seconds"),
            "sender": message.sender,
            "text": message.text,
        }
        for message in messages
    ]
    content = "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    ) + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"messages-{timestamp.strftime('%Y%m%d-%H%M%S-%f')}-{peer_id}-{safe_chat_name(title)}"
    temporary_path = output_dir / f".{stem}-{uuid.uuid4().hex}.tmp"
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        for index in count(1):
            suffix = "" if index == 1 else f"-{index}"
            path = output_dir / f"{stem}{suffix}.jsonl"
            try:
                os.link(temporary_path, path)
                return path
            except FileExistsError:
                continue
    finally:
        temporary_path.unlink(missing_ok=True)
