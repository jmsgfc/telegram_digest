from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ChatInfo:
    peer_id: int
    title: str
    kind: str


@dataclass(frozen=True)
class DigestMessage:
    message_id: int
    date: datetime
    sender: str
    text: str

