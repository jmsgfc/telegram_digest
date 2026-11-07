from __future__ import annotations

import os
import sys
from pathlib import Path


def runtime_dir() -> Path:
    """返回可写运行目录：开发环境为项目根目录，打包后为 EXE 所在目录。"""
    configured = os.environ.get("TELEGRAM_DIGEST_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]

