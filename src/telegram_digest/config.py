from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "selected_chats": [],
    "chat_groups": [],
    "initial_lookback_hours": 24,
    "max_messages_per_chat": 500,
}


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    api_id: int
    api_hash: str
    selected_chats: list[dict[str, Any]] = field(default_factory=list)
    initial_lookback_hours: int = 24
    max_messages_per_chat: int = 500

    @property
    def data_dir(self) -> Path:
        return self.project_dir / "data"

    @property
    def output_dir(self) -> Path:
        return self.project_dir / "output"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "telegram_digest"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_credentials(project_dir: Path) -> tuple[str, str]:
    values = _load_env(project_dir / ".env")
    return values.get("TELEGRAM_API_ID", ""), values.get("TELEGRAM_API_HASH", "")


def save_credentials(project_dir: Path, api_id: str, api_hash: str) -> None:
    api_id = api_id.strip()
    api_hash = api_hash.strip()
    if not api_id.isdigit():
        raise ValueError("API ID 必须是数字。")
    if len(api_hash) < 16:
        raise ValueError("API Hash 格式不正确。")
    path = project_dir / ".env"
    temporary = project_dir / ".env.tmp"
    temporary.write_text(
        "# 仅保存在本机，不要上传或分享\n"
        f"TELEGRAM_API_ID={api_id}\n"
        f"TELEGRAM_API_HASH={api_hash}\n",
        encoding="utf-8",
    )
    _chmod_if_posix(temporary, 0o600)
    temporary.replace(path)
    _chmod_if_posix(path, 0o600)


def ensure_runtime_files(project_dir: Path) -> None:
    data_dir = project_dir / "data"
    output_dir = project_dir / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    if not config_path.exists():
        _atomic_write_json(config_path, DEFAULT_CONFIG)
    else:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        legacy_keys = {"summarizer", "send_to_saved_messages"}.intersection(config)
        if legacy_keys:
            for key in legacy_keys:
                config.pop(key)
            _atomic_write_json(config_path, config)
    _secure_runtime_permissions(project_dir)


def _chmod_if_posix(path: Path, mode: int) -> None:
    if os.name == "posix" and path.exists():
        try:
            path.chmod(mode)
        except PermissionError:
            # 个别宿主机挂载不支持 POSIX chmod；Docker 仍通过 umask 077 保护新文件。
            pass


def _secure_runtime_permissions(project_dir: Path) -> None:
    """容器/Linux 中将凭据、会话和消息数据限制为当前用户可读写。"""
    if os.name != "posix":
        return
    for directory in (project_dir, project_dir / "data", project_dir / "output"):
        _chmod_if_posix(directory, 0o700)
    for path in project_dir.rglob("*"):
        _chmod_if_posix(path, 0o700 if path.is_dir() else 0o600)


def load_settings(project_dir: Path) -> Settings:
    ensure_runtime_files(project_dir)
    env = {**_load_env(project_dir / ".env"), **os.environ}
    raw_api_id = env.get("TELEGRAM_API_ID", "").strip()
    api_hash = env.get("TELEGRAM_API_HASH", "").strip()
    if not raw_api_id or not api_hash:
        raise ValueError("缺少 TELEGRAM_API_ID 或 TELEGRAM_API_HASH，请先填写 .env 文件。")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID 必须是整数。") from exc

    config = json.loads((project_dir / "data" / "config.json").read_text(encoding="utf-8"))
    return Settings(
        project_dir=project_dir,
        api_id=api_id,
        api_hash=api_hash,
        selected_chats=list(config.get("selected_chats", [])),
        initial_lookback_hours=max(1, int(config.get("initial_lookback_hours", 24))),
        max_messages_per_chat=max(1, int(config.get("max_messages_per_chat", 500))),
    )


def save_selected_chats(project_dir: Path, chats: list[dict[str, Any]]) -> None:
    ensure_runtime_files(project_dir)
    config_path = project_dir / "data" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["selected_chats"] = chats
    _atomic_write_json(config_path, config)


def read_runtime_config(project_dir: Path) -> dict[str, Any]:
    ensure_runtime_files(project_dir)
    config_path = project_dir / "data" / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("运行配置文件无法读取。") from exc


def update_runtime_config(project_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    config = read_runtime_config(project_dir)
    config.update(updates)
    _atomic_write_json(project_dir / "data" / "config.json", config)
    return config


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)

