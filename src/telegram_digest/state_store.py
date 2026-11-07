from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"状态文件无法读取：{self.path}") from exc
        return {str(key): int(value) for key, value in raw.items()}

    def update_many(self, updates: dict[int, int]) -> None:
        state = self.load()
        for peer_id, message_id in updates.items():
            state[str(peer_id)] = int(message_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

