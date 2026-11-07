from __future__ import annotations

import os
from pathlib import Path


def runtime_dir() -> Path:
    """返回可写运行目录：默认使用项目根目录，Docker 可通过环境变量覆盖。"""
    configured = os.environ.get("TELEGRAM_DIGEST_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]

