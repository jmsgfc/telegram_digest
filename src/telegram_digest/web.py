from __future__ import annotations

import asyncio
import io
import time
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

from .config import (
    ensure_runtime_files,
    load_settings,
    read_credentials,
    read_runtime_config,
    save_credentials,
    save_selected_chats,
    update_runtime_config,
)
from .digest import export_selected_messages
from .models import ChatInfo
from .paths import runtime_dir
from .phone import normalize_phone
from .telegram_client import create_client, list_group_dialogs


RUNTIME_DIR = runtime_dir()
STATIC_DIR = Path(__file__).resolve().parent / "web_static"
PENDING_TTL_SECONDS = 600

app = FastAPI(title="Telegram Digest", docs_url=None, redoc_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver", "telegram-digest"],
)

telegram_lock = asyncio.Lock()
group_lock = asyncio.Lock()
pending_login: dict[str, Any] = {}
chat_cache: dict[int, ChatInfo] = {}
jobs: dict[str, dict[str, Any]] = {}


@app.middleware("http")
async def local_request_guard(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("X-Telegram-Digest") != "local-web-ui":
            return JSONResponse(status_code=403, content={"detail": "拒绝跨站写入请求。"})
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class CredentialsPayload(BaseModel):
    api_id: str = Field(min_length=1, max_length=20)
    api_hash: str = Field(min_length=16, max_length=128)


class CodePayload(BaseModel):
    country_code: str = Field(min_length=1, max_length=8)
    phone_number: str = Field(min_length=5, max_length=32)


class LoginPayload(BaseModel):
    code: str = Field(default="", max_length=16)
    password: str = Field(default="", max_length=256)


class SelectionPayload(BaseModel):
    peer_ids: list[int] = Field(max_length=500)


class GroupPayload(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    peer_ids: list[int] = Field(min_length=1, max_length=500)


class SettingsPayload(BaseModel):
    initial_lookback_hours: int = Field(ge=1, le=720)
    max_messages_per_chat: int = Field(ge=1, le=5000)


class DigestArchivePayload(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=100)


@app.on_event("startup")
async def startup() -> None:
    ensure_runtime_files(RUNTIME_DIR)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    api_id, api_hash = read_credentials(RUNTIME_DIR)
    config = read_runtime_config(RUNTIME_DIR)
    return {
        "configured": bool(api_id and api_hash),
        "session_exists": (RUNTIME_DIR / "data" / "telegram_digest.session").exists(),
        "selected_chats": config.get("selected_chats", []),
        "settings": {
            "initial_lookback_hours": int(config.get("initial_lookback_hours", 24)),
            "max_messages_per_chat": int(config.get("max_messages_per_chat", 500)),
        },
    }


@app.put("/api/credentials")
async def put_credentials(payload: CredentialsPayload) -> dict[str, bool]:
    try:
        save_credentials(RUNTIME_DIR, payload.api_id, payload.api_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"configured": True}


@app.get("/api/account")
async def account() -> dict[str, Any]:
    settings = _settings_or_http_error()
    async with telegram_lock:
        client = create_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {"authorized": False}
            me = await client.get_me()
            return {
                "authorized": True,
                "account": {
                    "id": int(me.id),
                    "name": (me.first_name or "") + ((" " + me.last_name) if me.last_name else ""),
                    "username": me.username or "",
                },
            }
        except (RPCError, OSError) as exc:
            raise _telegram_http_error(exc) from exc
        finally:
            await client.disconnect()


@app.post("/api/auth/code")
async def send_code(payload: CodePayload) -> dict[str, Any]:
    try:
        phone = normalize_phone(payload.country_code, payload.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    settings = _settings_or_http_error()
    async with telegram_lock:
        client = create_client(settings)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {"already_authorized": True, "name": me.first_name or str(me.id)}
            sent = await client.send_code_request(phone)
            pending_login.clear()
            pending_login.update(
                {
                    "phone": phone,
                    "phone_code_hash": sent.phone_code_hash,
                    "expires_at": time.monotonic() + PENDING_TTL_SECONDS,
                }
            )
            return {"code_sent": True, "expires_in": PENDING_TTL_SECONDS}
        except (RPCError, OSError) as exc:
            raise _telegram_http_error(exc) from exc
        finally:
            await client.disconnect()


@app.post("/api/auth/login")
async def complete_login(payload: LoginPayload) -> dict[str, Any]:
    settings = _settings_or_http_error()
    async with telegram_lock:
        client = create_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                _require_pending_login()
                if not payload.code.strip():
                    raise HTTPException(status_code=422, detail="请填写验证码。")
                try:
                    await client.sign_in(
                        pending_login["phone"],
                        payload.code.strip(),
                        phone_code_hash=pending_login["phone_code_hash"],
                    )
                except SessionPasswordNeededError:
                    if not payload.password:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "PASSWORD_REQUIRED", "message": "该账号已开启两步验证。"},
                        )
                    await client.sign_in(password=payload.password)
            me = await client.get_me()
            pending_login.clear()
            return {
                "authorized": True,
                "account": {
                    "id": int(me.id),
                    "name": (me.first_name or "") + ((" " + me.last_name) if me.last_name else ""),
                    "username": me.username or "",
                },
            }
        except HTTPException:
            raise
        except (RPCError, OSError) as exc:
            raise _telegram_http_error(exc) from exc
        finally:
            await client.disconnect()


@app.get("/api/chats")
async def get_chats() -> dict[str, Any]:
    settings = _settings_or_http_error()
    async with telegram_lock:
        chats = await _fetch_chats(settings)
    selected = {int(item["peer_id"]) for item in settings.selected_chats}
    return {
        "chats": [
            {**asdict(chat), "selected": chat.peer_id in selected}
            for chat in chats
        ]
    }


@app.put("/api/chats/selection")
async def put_selection(payload: SelectionPayload) -> dict[str, int]:
    settings = _settings_or_http_error()
    requested_ids = list(dict.fromkeys(payload.peer_ids))
    requested = set(requested_ids)
    async with telegram_lock:
        chats = list(chat_cache.values()) or await _fetch_chats(settings)
        available = {chat.peer_id: chat for chat in chats}
        unknown = requested.difference(available)
        if unknown:
            raise HTTPException(status_code=422, detail="选择中包含无效或已退出的会话。")
        selected = [asdict(available[peer_id]) for peer_id in requested_ids]
        save_selected_chats(RUNTIME_DIR, selected)
    return {"selected_count": len(selected)}


@app.get("/api/chat-groups")
async def get_chat_groups() -> dict[str, Any]:
    return {"groups": _chat_groups_or_http_error()}


@app.put("/api/chat-groups")
async def put_chat_group(payload: GroupPayload) -> dict[str, Any]:
    name = _clean_group_name(payload.name)
    peer_ids = list(dict.fromkeys(payload.peer_ids))
    settings = _settings_or_http_error()
    async with telegram_lock:
        chats = list(chat_cache.values()) or await _fetch_chats(settings)
        available = {chat.peer_id: chat for chat in chats}
        unknown = set(peer_ids).difference(available)
        if unknown:
            raise HTTPException(status_code=422, detail="分组中包含无效或已退出的会话。")

    async with group_lock:
        groups = _chat_groups_or_http_error()
        existing = next((item for item in groups if item["name"].casefold() == name.casefold()), None)
        if existing:
            existing["name"] = name
            existing["peer_ids"] = peer_ids
            saved_id = existing["id"]
        else:
            if len(groups) >= 50:
                raise HTTPException(status_code=422, detail="偏好分组最多保存 50 个。")
            saved_id = uuid.uuid4().hex
            groups.append({"id": saved_id, "name": name, "peer_ids": peer_ids})
        update_runtime_config(RUNTIME_DIR, {"chat_groups": groups})
    return {"saved_id": saved_id, "groups": groups}


@app.delete("/api/chat-groups/{group_id}")
async def delete_chat_group(group_id: str) -> dict[str, Any]:
    async with group_lock:
        groups = _chat_groups_or_http_error()
        remaining = [item for item in groups if item["id"] != group_id]
        if len(remaining) == len(groups):
            raise HTTPException(status_code=404, detail="偏好分组不存在。")
        update_runtime_config(RUNTIME_DIR, {"chat_groups": remaining})
    return {"groups": remaining}


@app.put("/api/settings")
async def put_settings(payload: SettingsPayload) -> dict[str, bool]:
    update_runtime_config(
        RUNTIME_DIR,
        {
            "initial_lookback_hours": payload.initial_lookback_hours,
            "max_messages_per_chat": payload.max_messages_per_chat,
        },
    )
    return {"saved": True}


@app.post("/api/digests", status_code=202)
async def start_digest() -> dict[str, str]:
    settings = _settings_or_http_error()
    if not settings.selected_chats:
        raise HTTPException(status_code=422, detail="请先选择需要导出的群聊或频道。")
    if any(job["status"] in {"queued", "running"} for job in jobs.values()):
        raise HTTPException(status_code=409, detail="已有消息导出任务正在运行。")
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "messages": [],
        "results": [],
        "warnings": [],
        "error": None,
        "created_at": time.time(),
    }
    asyncio.create_task(_run_digest_job(job_id))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启。")
    return job


@app.get("/api/digests")
async def list_digests() -> dict[str, Any]:
    output_dir = RUNTIME_DIR / "output"
    ensure_runtime_files(RUNTIME_DIR)
    files = sorted(output_dir.glob("messages-*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
    return {
        "digests": [
            {
                "name": item.name,
                "size": item.stat().st_size,
                "modified": item.stat().st_mtime,
            }
            for item in files[:100]
        ]
    }


@app.post("/api/digests/archive")
async def download_digest_archive(payload: DigestArchivePayload):
    filenames = list(dict.fromkeys(payload.filenames))
    paths: list[Path] = []
    total_size = 0
    for filename in filenames:
        path = _safe_data_path(filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="消息数据文件不存在。")
        total_size += path.stat().st_size
        if total_size > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="所选数据文件总大小不能超过 100 MB。")
        paths.append(path)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.writestr(path.name, path.read_bytes())
    buffer.seek(0)
    filename = f"telegram-messages-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/digests/{filename}")
async def get_digest(filename: str, download: bool = Query(default=False)):
    path = _safe_data_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="消息数据文件不存在。")
    if download:
        return FileResponse(path, filename=path.name, media_type="application/x-ndjson; charset=utf-8")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/x-ndjson; charset=utf-8")


async def _fetch_chats(settings) -> list[ChatInfo]:
    client = create_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="账号尚未登录。")
        chats = await list_group_dialogs(client)
        chat_cache.clear()
        chat_cache.update({chat.peer_id: chat for chat in chats})
        return chats
    except HTTPException:
        raise
    except (RPCError, OSError) as exc:
        raise _telegram_http_error(exc) from exc
    finally:
        await client.disconnect()


async def _run_digest_job(job_id: str) -> None:
    job = jobs[job_id]
    job["status"] = "running"

    def report(message: str) -> None:
        job["messages"].append(message)

    try:
        async with telegram_lock:
            result = await export_selected_messages(RUNTIME_DIR, reporter=report)
        job["results"] = [path.name for path in result.paths]
        job["warnings"] = result.warnings
        if result.warnings:
            job["error"] = "；".join(result.warnings)
            job["status"] = "partial" if result.paths else "failed"
        else:
            job["status"] = "succeeded"
    except FloodWaitError as exc:
        job["status"] = "failed"
        job["error"] = f"Telegram 限流，请等待 {exc.seconds} 秒后重试。"
    except (ValueError, RuntimeError, OSError, RPCError) as exc:
        job["status"] = "failed"
        job["error"] = _safe_error_message(exc)
    except Exception:
        # 后台任务不能把内部异常或敏感信息暴露给浏览器，也不能永久停留在 running。
        job["status"] = "failed"
        job["error"] = "导出消息时发生内部错误，请查看容器日志。"


def _settings_or_http_error():
    try:
        return load_settings(RUNTIME_DIR)
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _clean_group_name(value: str) -> str:
    name = value.strip()
    if not name or any(ord(character) < 32 for character in name):
        raise HTTPException(status_code=422, detail="分组名称格式不正确。")
    return name


def _chat_groups_or_http_error() -> list[dict[str, Any]]:
    raw = read_runtime_config(RUNTIME_DIR).get("chat_groups", [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="偏好分组配置格式错误。")
    groups: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="偏好分组配置格式错误。")
        group_id = item.get("id")
        name = item.get("name")
        peer_ids = item.get("peer_ids")
        if not isinstance(group_id, str) or not isinstance(name, str) or not isinstance(peer_ids, list):
            raise HTTPException(status_code=400, detail="偏好分组配置格式错误。")
        if not all(isinstance(peer_id, int) for peer_id in peer_ids):
            raise HTTPException(status_code=400, detail="偏好分组配置格式错误。")
        groups.append({"id": group_id, "name": name, "peer_ids": list(dict.fromkeys(peer_ids))})
    return groups


def _require_pending_login() -> None:
    if not pending_login or time.monotonic() >= float(pending_login.get("expires_at", 0)):
        pending_login.clear()
        raise HTTPException(status_code=409, detail="验证码会话已失效，请重新发送验证码。")


def _safe_data_path(filename: str) -> Path:
    if not filename.startswith("messages-") or not filename.endswith(".jsonl") or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="消息数据文件不存在。")
    output_dir = (RUNTIME_DIR / "output").resolve()
    path = (output_dir / filename).resolve()
    if path.parent != output_dir:
        raise HTTPException(status_code=404, detail="消息数据文件不存在。")
    return path


def _telegram_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FloodWaitError):
        return HTTPException(status_code=429, detail=f"操作过于频繁，请等待 {exc.seconds} 秒。")
    if isinstance(exc, PhoneNumberInvalidError):
        return HTTPException(status_code=422, detail="手机号无效，请检查区号和号码。")
    if isinstance(exc, PhoneCodeInvalidError):
        return HTTPException(status_code=422, detail="验证码不正确。")
    if isinstance(exc, PhoneCodeExpiredError):
        return HTTPException(status_code=409, detail="验证码已过期，请重新发送。")
    if isinstance(exc, PasswordHashInvalidError):
        return HTTPException(status_code=422, detail="两步验证密码不正确。")
    return HTTPException(status_code=502, detail="无法连接 Telegram，请稍后重试。")


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, FloodWaitError):
        return f"Telegram 限流，请等待 {exc.seconds} 秒。"
    if isinstance(exc, ValueError):
        return str(exc)
    return "消息导出失败，请检查网络和配置后重试。"


@app.exception_handler(Exception)
async def unhandled_exception(_request, _exc):
    return JSONResponse(status_code=500, content={"detail": "服务发生未预期错误。"})


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def run() -> None:
    uvicorn.run("telegram_digest.web:app", host="127.0.0.1", port=8765, workers=1)


if __name__ == "__main__":
    run()
