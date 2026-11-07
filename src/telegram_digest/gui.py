from __future__ import annotations

import asyncio
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Coroutine

from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError

from .cli import DigestRunResult, command_digest
from .config import (
    ensure_runtime_files,
    load_settings,
    read_credentials,
    save_credentials,
    save_selected_chats,
)
from .models import ChatInfo
from .paths import runtime_dir
from .phone import normalize_phone
from .telegram_client import create_client, list_group_dialogs


PROJECT_DIR = runtime_dir()


class DigestApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Telegram 个人消息导出")
        self.root.geometry("920x720")
        self.root.minsize(760, 600)
        self.phone_code_hash: str | None = None
        self.loaded_chats: list[ChatInfo] = []
        self.busy = False
        self.closing = False
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()

        ensure_runtime_files(PROJECT_DIR)
        api_id, api_hash = read_credentials(PROJECT_DIR)
        self.api_id_var = tk.StringVar(value=api_id)
        self.api_hash_var = tk.StringVar(value=api_hash)
        self.country_code_var = tk.StringVar(value="+86")
        self.phone_number_var = tk.StringVar()
        self.code_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._drain_events)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        account = ttk.LabelFrame(container, text="账号登录", padding=10)
        account.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for column in (1, 3):
            account.columnconfigure(column, weight=1)

        ttk.Label(account, text="API ID").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(account, textvariable=self.api_id_var).grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=4)
        ttk.Label(account, text="API Hash").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(account, textvariable=self.api_hash_var, show="●").grid(row=0, column=3, sticky="ew", pady=4)

        ttk.Label(account, text="区号").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(account, textvariable=self.country_code_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 12), pady=4
        )
        ttk.Label(account, text="手机号").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(account, textvariable=self.phone_number_var).grid(row=1, column=3, sticky="ew", pady=4)
        self.send_code_button = ttk.Button(account, text="保存并发送验证码", command=self.send_code)
        self.send_code_button.grid(row=2, column=0, columnspan=4, sticky="ew", pady=4)

        ttk.Label(account, text="验证码").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(account, textvariable=self.code_var).grid(row=3, column=1, sticky="ew", padx=(0, 12), pady=4)
        ttk.Label(account, text="两步验证密码（可空）").grid(row=3, column=2, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(account, textvariable=self.password_var, show="●").grid(row=3, column=3, sticky="ew", pady=4)

        self.login_button = ttk.Button(account, text="完成登录 / 检查登录状态", command=self.complete_login)
        self.login_button.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        middle = ttk.Panedwindow(container, orient=tk.HORIZONTAL)
        middle.grid(row=1, column=0, sticky="nsew")

        chats_frame = ttk.LabelFrame(middle, text="群聊与频道", padding=10)
        logs_frame = ttk.LabelFrame(middle, text="运行日志", padding=10)
        middle.add(chats_frame, weight=3)
        middle.add(logs_frame, weight=2)

        chats_frame.rowconfigure(0, weight=1)
        chats_frame.columnconfigure(0, weight=1)
        self.chat_list = tk.Listbox(chats_frame, selectmode=tk.EXTENDED, exportselection=False)
        chat_scroll = ttk.Scrollbar(chats_frame, orient=tk.VERTICAL, command=self.chat_list.yview)
        self.chat_list.configure(yscrollcommand=chat_scroll.set)
        self.chat_list.grid(row=0, column=0, sticky="nsew")
        chat_scroll.grid(row=0, column=1, sticky="ns")

        chat_buttons = ttk.Frame(chats_frame)
        chat_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        chat_buttons.columnconfigure((0, 1), weight=1)
        self.load_button = ttk.Button(chat_buttons, text="加载会话", command=self.load_chats)
        self.load_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.save_button = ttk.Button(chat_buttons, text="保存选择", command=self.save_chat_selection)
        self.save_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        logs_frame.rowconfigure(0, weight=1)
        logs_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logs_frame, wrap=tk.WORD, state=tk.DISABLED, height=18)
        log_scroll = ttk.Scrollbar(logs_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(container)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure((0, 1), weight=1)
        self.digest_button = ttk.Button(actions, text="导出新增消息", command=self.generate_digest)
        self.digest_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(actions, text="打开数据目录", command=lambda: os.startfile(PROJECT_DIR / "output")).grid(
            row=0, column=1, sticky="ew", padx=(5, 0)
        )
        ttk.Label(container, textvariable=self.status_var, anchor="w").grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def _save_api_fields(self) -> None:
        save_credentials(PROJECT_DIR, self.api_id_var.get(), self.api_hash_var.get())

    def send_code(self) -> None:
        try:
            phone = normalize_phone(self.country_code_var.get(), self.phone_number_var.get())
        except ValueError as exc:
            messagebox.showerror("手机号错误", str(exc))
            return
        try:
            self._save_api_fields()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self._run_async("正在发送验证码……", lambda: self._send_code_task(phone), self._code_sent)

    async def _send_code_task(self, phone: str) -> dict[str, Any]:
        settings = load_settings(PROJECT_DIR)
        client = create_client(settings)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {"authorized": True, "name": me.first_name or str(me.id)}
            sent = await client.send_code_request(phone)
            return {"authorized": False, "phone_code_hash": sent.phone_code_hash}
        finally:
            await client.disconnect()

    def _code_sent(self, result: dict[str, Any]) -> None:
        if result["authorized"]:
            self._log(f"账号已经登录：{result['name']}")
            messagebox.showinfo("登录状态", "当前账号已经登录。")
            return
        self.phone_code_hash = result["phone_code_hash"]
        self._log("验证码已发送，请查看 Telegram 客户端。")
        messagebox.showinfo("验证码已发送", "请填写 Telegram 客户端收到的验证码。")

    def complete_login(self) -> None:
        try:
            self._save_api_fields()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        raw_phone_number = self.phone_number_var.get().strip()
        if raw_phone_number:
            try:
                phone = normalize_phone(self.country_code_var.get(), raw_phone_number)
            except ValueError as exc:
                messagebox.showerror("手机号错误", str(exc))
                return
        else:
            phone = ""
        code = self.code_var.get().strip()
        password = self.password_var.get()
        self._run_async(
            "正在登录……",
            lambda: self._login_task(phone, code, password),
            self._login_finished,
        )

    async def _login_task(self, phone: str, code: str, password: str) -> str:
        settings = load_settings(PROJECT_DIR)
        client = create_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                if not phone or not code or not self.phone_code_hash:
                    raise ValueError("请先发送验证码，再填写验证码完成登录。")
                try:
                    await client.sign_in(phone, code, phone_code_hash=self.phone_code_hash)
                except SessionPasswordNeededError:
                    if not password:
                        raise ValueError("该账号已开启两步验证，请填写两步验证密码。")
                    await client.sign_in(password=password)
            me = await client.get_me()
            return me.first_name or str(me.id)
        finally:
            await client.disconnect()

    def _login_finished(self, name: str) -> None:
        self.code_var.set("")
        self.password_var.set("")
        self.phone_code_hash = None
        self._log(f"登录成功：{name}")
        messagebox.showinfo("登录成功", f"当前账号：{name}")

    def load_chats(self) -> None:
        self._run_async("正在加载群聊和频道……", self._load_chats_task, self._chats_loaded)

    async def _load_chats_task(self) -> list[ChatInfo]:
        settings = load_settings(PROJECT_DIR)
        client = create_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise ValueError("账号尚未登录。")
            return await list_group_dialogs(client)
        finally:
            await client.disconnect()

    def _chats_loaded(self, chats: list[ChatInfo]) -> None:
        self.loaded_chats = chats
        self.chat_list.delete(0, tk.END)
        selected_ids = {int(item["peer_id"]) for item in load_settings(PROJECT_DIR).selected_chats}
        for index, chat in enumerate(chats):
            self.chat_list.insert(tk.END, f"[{chat.kind}] {chat.title}   ID={chat.peer_id}")
            if chat.peer_id in selected_ids:
                self.chat_list.selection_set(index)
        self._log(f"已加载 {len(chats)} 个群聊或频道。")

    def save_chat_selection(self) -> None:
        indexes = self.chat_list.curselection()
        if not indexes:
            messagebox.showwarning("尚未选择", "请至少选择一个群聊或频道。")
            return
        selected = [
            {
                "peer_id": self.loaded_chats[index].peer_id,
                "title": self.loaded_chats[index].title,
                "kind": self.loaded_chats[index].kind,
            }
            for index in indexes
        ]
        save_selected_chats(PROJECT_DIR, selected)
        self._log(f"已保存 {len(selected)} 个会话。")
        messagebox.showinfo("保存成功", f"已选择 {len(selected)} 个会话。")

    def generate_digest(self) -> None:
        self._run_async(
            "正在读取并导出新增消息……",
            lambda: command_digest(reporter=self._log),
            self._digest_finished,
        )

    def _digest_finished(self, result: DigestRunResult) -> None:
        if result.paths:
            warning = f"\n\n注意：{'；'.join(result.warnings)}" if result.warnings else ""
            messagebox.showinfo(
                "导出完成",
                f"已按会话生成 {len(result.paths)} 份 JSONL 文件：\n{result.paths[0].parent}{warning}",
            )
        elif result.warnings:
            messagebox.showwarning("导出未完成", "；".join(result.warnings))
        else:
            messagebox.showinfo("没有新消息", "选定会话没有需要处理的新消息。")

    def _run_async(
        self,
        status: str,
        coroutine_factory: Callable[[], Coroutine[Any, Any, Any]],
        on_success: Callable[[Any], None],
    ) -> None:
        if self.busy:
            messagebox.showwarning("任务正在运行", "请等待当前任务完成。")
            return
        self.busy = True
        self.status_var.set(status)
        self._set_controls_enabled(False)

        def worker() -> None:
            try:
                result = asyncio.run(coroutine_factory())
            except FloodWaitError as exc:
                self.events.put(("failure", f"Telegram 要求等待 {exc.seconds} 秒后再试。"))
            except (RPCError, ValueError, RuntimeError, OSError) as exc:
                self.events.put(("failure", str(exc)))
            except Exception as exc:
                self.events.put(("failure", f"未预期错误：{type(exc).__name__}: {exc}"))
            else:
                self.events.put(("success", result, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _task_succeeded(self, result: Any, on_success: Callable[[Any], None]) -> None:
        self.busy = False
        self.status_var.set("完成")
        self._set_controls_enabled(True)
        on_success(result)

    def _task_failed(self, message: str) -> None:
        self.busy = False
        self.status_var.set("失败")
        self._set_controls_enabled(True)
        self._log(f"错误：{message}")
        messagebox.showerror("操作失败", message)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for button in (
            self.send_code_button,
            self.login_button,
            self.load_button,
            self.save_button,
            self.digest_button,
        ):
            button.configure(state=state)

    def _log(self, message: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.events.put(("log", message))
            return
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _drain_events(self) -> None:
        if self.closing:
            return
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "log":
                self._log(event[1])
            elif event[0] == "failure":
                self._task_failed(event[1])
            elif event[0] == "success":
                self._task_succeeded(event[1], event[2])
        self.root.after(100, self._drain_events)

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno("任务正在运行", "当前任务尚未完成，确定关闭吗？"):
            return
        self.closing = True
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    try:
        DigestApp(root)
    except OSError as exc:
        messagebox.showerror(
            "无法启动",
            f"程序目录不可写，无法创建配置或数据文件：\n{exc}\n\n请将压缩包完整解压到桌面或文档目录后重试。",
        )
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()

