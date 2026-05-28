# dsv4-cc-proxy-tray — Windows 系统托盘 GUI 启动器
#
# 在后台 daemon 线程中内嵌运行 uvicorn，不启动子进程。
# 避免 PyInstaller exe 中子进程重新触发 GUI 的循环启动问题。

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# ── 版本（独立定义，不 import proxy 模块） ─────────────────
try:
    from dsv4_cc_proxy._version import VERSION as _ver

    VERSION = _ver
except ImportError:
    _VERSION_PATH = Path(__file__).resolve().parent / "_version.py"
    _VERSION = {}
    if _VERSION_PATH.exists():
        exec(_VERSION_PATH.read_text(encoding="utf-8"), _VERSION)
    VERSION = _VERSION.get("VERSION", "?.?.?")

# ── 默认配置 ─────────────────────────────────────────────
_DEFAULT_UPSTREAM = "https://api.deepseek.com/anthropic"
_DEFAULT_LISTEN = "127.0.0.1:16889"
_DEFAULT_LOG_LEVEL = "warning"

# ── 持久化配置路径 ──────────────────────────────────────────
_CONFIG_PATH = Path.home() / ".dsv4-cc-proxy-tray.json"
_GUI_LOCKFILE = Path.home() / ".dsv4-cc-proxy-tray.lock"


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 日志颜色标记 ─────────────────────────────────────────────

_COLOR_TAGS = {
    "ERROR": "red",
    "CRITICAL": "darkred",
    "WARNING": "orange",
    "INFO": "black",
    "DEBUG": "gray",
}

_ROUTE_COLORS = {
    "CC": {"tag": "cc-route", "color": "#8B4513"},      # 棕色 — Claude Code
    "CODEX": {"tag": "codex-route", "color": "#2E8B57"},  # 绿色 — Codex
    "CHAT": {"tag": "codex-route", "color": "#2E8B57"},   # 绿色 — Codex chat
}

_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(\w+)\s+(.*)$")

_ROUTE_RE = re.compile(r"\[(CC|CODEX|CHAT)-")


# ── 跨平台进程检查 ──────────────────────────────────────────


def _is_process_running(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(
                f'tasklist /FI "PID eq {pid}" /FO CSV',
                shell=True,
                stderr=subprocess.DEVNULL,
            ).decode("gbk", errors="ignore")
            return f'"{pid}"' in output
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ── 日志捕获 Handler ──────────────────────────────────────


class _QueueHandler(logging.Handler):
    """将 logging 记录转发到 tkinter 日志队列。"""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord):
        self.q.put(self.format(record))


# ── GUI ──────────────────────────────────────────────────────


def main():
    # 全局异常捕获：错误写入文件以便调试无控制台的 exe
    _ERROR_LOG = Path.home() / ".dsv4-cc-proxy-tray-error.log"

    def _excepthook(exc_type, exc_val, exc_tb):
        import traceback

        _ERROR_LOG.write_text("".join(traceback.format_exception(exc_type, exc_val, exc_tb)), encoding="utf-8")
        sys.__excepthook__(exc_type, exc_val, exc_tb)

    sys.excepthook = _excepthook

    import tkinter as tk
    from tkinter import messagebox, ttk

    # ── 单实例保护 ──
    if _GUI_LOCKFILE.exists():
        try:
            old_pid = int(_GUI_LOCKFILE.read_text().strip())
            if _is_process_running(old_pid):
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0, "dsv4-cc-proxy-tray 已在运行中，请查看系统托盘或任务栏。", "提示", 0x40
                )
                sys.exit(0)
            else:
                _GUI_LOCKFILE.unlink(missing_ok=True)
        except (ValueError, OSError):
            _GUI_LOCKFILE.unlink(missing_ok=True)

    _GUI_LOCKFILE.write_text(str(os.getpid()))

    saved = _load_config()

    # ── 状态 ──
    _server_stop = threading.Event()
    _server = None  # uvicorn.Server
    _server_thread = None  # threading.Thread
    log_queue: queue.Queue = queue.Queue()

    def _parse_host_port(s: str) -> tuple[str, int]:
        s = s.strip()
        if ":" in s:
            host, port_str = s.rsplit(":", 1)
            return host, int(port_str)
        return s, 16889

    # ── 环境变量（在启动线程前设置） ──

    def _set_env(upstream: str, host: str, port: int, log_level: str):
        os.environ["PROXY_UPSTREAM"] = upstream
        os.environ["PROXY_HOST"] = host
        os.environ["PROXY_PORT"] = str(port)
        os.environ["PROXY_LOG_LEVEL"] = log_level
        os.environ["PROXY_GUI_MODE"] = "1"

    # ── 启动 / 停止 ──

    def _setup_logging(log_level: str):
        handler = _QueueHandler(log_queue)
        lvl = getattr(logging, log_level.upper(), logging.WARNING)
        # 根 logger 的 level 控制 GUI 中可见的最低日志级别
        root_logger = logging.getLogger()
        root_logger.handlers = [handler]
        root_logger.setLevel(lvl)
        # proxy.py 用 logger.getLogger("deepseek-proxy") 写业务日志，
        # 确保其 propagate=True 且 level ≤ log_level。
        plog = logging.getLogger("deepseek-proxy")
        plog.propagate = True
        plog.setLevel(logging.DEBUG)

    def start():
        nonlocal _server, _server_thread

        if _server is not None or (_server_thread is not None and _server_thread.is_alive()):
            messagebox.showinfo("提示", "代理已在运行中")
            return

        upstream = upstream_var.get()
        listen_addr = listen_var.get()
        log_level = log_level_var.get()
        host, port = _parse_host_port(listen_addr)
        _set_env(upstream, host, port, log_level)

        _save_config(
            {
                "upstream": upstream,
                "listen": listen_addr,
                "log_level": log_level,
            }
        )

        _setup_logging(log_level)
        _append_text(f"启动代理 v{VERSION}\n", "INFO")

        # 在主线程 import，确保环境变量已设置
        try:
            import uvicorn

            from dsv4_cc_proxy.proxy import create_app

            # log_config=None 阻止 uvicorn 用 dictConfig 覆盖 root logger
            # log_config=None 阻止 uvicorn 覆盖 root handler。
            # uvicorn log_level 固定 info 以确保启动日志输出；
            # 用户可在 GUI 下拉框控制 root logger 最低可见级别。
            config = uvicorn.Config(
                app=create_app(),
                host=host,
                port=port,
                log_level="info",
                log_config=None,
            )
            _server = uvicorn.Server(config)
        except Exception:
            import traceback

            err = traceback.format_exc()
            _ERROR_LOG.write_text(err, encoding="utf-8")
            _append_text(f"启动失败:\n{err}\n", "ERROR")
            return

        def _run_server():
            nonlocal _server, _server_thread
            try:
                _server.run()
            except Exception:
                logging.getLogger().exception("uvicorn error")
            _server = None
            _server_thread = None
            _update_status()
            _append_text("代理已停止\n", "INFO")

        _server_thread = threading.Thread(target=_run_server, daemon=True)
        _server_thread.start()
        _update_status()

    def stop():
        nonlocal _server
        if _server is None:
            messagebox.showinfo("提示", "代理未在运行")
            return
        _append_text("正在停止代理...\n", "INFO")
        _server.should_exit = True
        _update_status()

    def _update_status():
        def _do():
            running = _server is not None
            status_label.config(text="● 运行中" if running else "○ 已停止", foreground="green" if running else "gray")
            start_btn.config(state="disabled" if running else "normal")
            stop_btn.config(state="normal" if running else "disabled")

        root.after(0, _do)

    # ── 日志轮询（批量插入，避免高频操作阻塞 GUI） ──

    _MAX_LINES = 5000
    _TRIM_TO = 4000
    _log_buffer: list[tuple[str, str, str]] = []  # (ts, msg, color_tag)
    _tags_inited = False

    def _init_tags():
        nonlocal _tags_inited
        if _tags_inited:
            return
        log_text.tag_config("ts", foreground="gray")
        for level, c in _COLOR_TAGS.items():
            log_text.tag_config(c, foreground=c)
        for route, info in _ROUTE_COLORS.items():
            log_text.tag_config(info["tag"], foreground=info["color"])
        _tags_inited = True

    def _flush_log():
        if not _log_buffer:
            return

        _init_tags()
        log_text.config(state="normal")
        for ts, msg, color in _log_buffer:
            tag = (color,) if color != "black" else ()
            log_text.insert("end", ts, ("ts",))
            log_text.insert("end", msg + "\n", tag)
        _log_buffer.clear()

        total = int(log_text.index("end-1c").split(".")[0])
        if total > _MAX_LINES:
            cutoff = total - _TRIM_TO
            log_text.delete("1.0", f"{cutoff + 1}.0")

        log_text.config(state="disabled")
        log_text.see("end")

    def _poll_log():
        try:
            while True:
                raw = log_queue.get_nowait()
                _append_text(raw)
        except queue.Empty:
            pass
        _flush_log()
        root.after(500, _poll_log)

    def _route_color(msg: str) -> str | None:
        m = _ROUTE_RE.search(msg)
        if m:
            info = _ROUTE_COLORS.get(m.group(1))
            return info["tag"] if info else None
        return None

    def _append_text(raw: str, force_level: str = ""):
        color = "black"
        ts = ""
        msg = raw.rstrip("\n")

        if force_level:
            color = _COLOR_TAGS.get(force_level, "black")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3] + " "
        else:
            m = _LOG_LINE_RE.match(raw)
            if m:
                ts = m.group(1) + " "
                color = _COLOR_TAGS.get(m.group(2), "black")
                msg = m.group(3)
            else:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3] + " "

        rc = _route_color(msg)
        if rc:
            color = rc

        _log_buffer.append((ts, msg, color))

    def clear_log():
        _log_buffer.clear()
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")

    def save_log():
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("全部", "*.*")],
        )
        if path:
            Path(path).write_text(log_text.get("1.0", "end"), encoding="utf-8")

    # ── 窗口根 ──

    root = tk.Tk()
    root.title(f"dsv4-cc-proxy-tray v{VERSION}")
    root.geometry("860x580")
    root.minsize(640, 400)
    try:
        root.iconbitmap(default="")
    except tk.TclError:
        pass

    # ── 配置区 ──

    cfg_frame = ttk.LabelFrame(root, text="配置", padding=8)
    cfg_frame.pack(fill="x", padx=10, pady=(10, 4))

    ttk.Label(cfg_frame, text="上游地址:").grid(row=0, column=0, sticky="e", padx=(0, 4))
    upstream_var = tk.StringVar(value=saved.get("upstream", _DEFAULT_UPSTREAM))
    ttk.Entry(cfg_frame, textvariable=upstream_var, width=60).grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)

    ttk.Label(cfg_frame, text="监听地址:").grid(row=1, column=0, sticky="e", padx=(0, 4))
    listen_var = tk.StringVar(value=saved.get("listen", _DEFAULT_LISTEN))
    ttk.Entry(cfg_frame, textvariable=listen_var, width=22).grid(row=1, column=1, sticky="w", pady=2)

    ttk.Label(cfg_frame, text="日志级别:").grid(row=2, column=0, sticky="e", padx=(0, 4))
    log_level_var = tk.StringVar(value=saved.get("log_level", _DEFAULT_LOG_LEVEL))
    levels = ["debug", "info", "warning", "error", "critical"]
    ttk.Combobox(cfg_frame, textvariable=log_level_var, values=levels, state="readonly", width=10).grid(
        row=2, column=1, sticky="w", pady=2
    )

    cfg_frame.columnconfigure(1, weight=1)

    # ── 按钮行 ──

    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill="x", padx=10, pady=4)

    start_btn = ttk.Button(btn_frame, text="▶ 启动", command=start)
    start_btn.pack(side="left", padx=(0, 6))
    stop_btn = ttk.Button(btn_frame, text="⏹ 停止", command=stop)
    stop_btn.pack(side="left", padx=(0, 12))

    status_label = ttk.Label(btn_frame, text="○ 已停止", foreground="gray")
    status_label.pack(side="left", padx=(0, 12))

    ttk.Label(btn_frame, text="路由: Claude Code + Codex").pack(side="left", padx=(0, 0))

    # ── 日志区 ──

    log_frame = ttk.LabelFrame(root, text="日志", padding=4)
    log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 4))

    log_text = tk.Text(log_frame, state="disabled", wrap="word", font=("Consolas", 9), relief="sunken", borderwidth=1)
    log_scroll = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)

    log_text.pack(side="left", fill="both", expand=True)
    log_scroll.pack(side="right", fill="y")

    # ── 日志底部按钮 ──

    log_btn_frame = ttk.Frame(root)
    log_btn_frame.pack(fill="x", padx=10, pady=(0, 8))

    ttk.Button(log_btn_frame, text="清空日志", command=clear_log).pack(side="left", padx=(0, 6))
    ttk.Button(log_btn_frame, text="保存日志", command=save_log).pack(side="left")

    # ── 窗口关闭 ──

    def on_close():
        running = _server is not None
        if running:
            if messagebox.askokcancel("退出", "代理正在运行，确定退出并停止代理？"):
                stop()
                root.after(300, root.destroy)
        else:
            root.destroy()

    def _cleanup_lock():
        _GUI_LOCKFILE.unlink(missing_ok=True)

    root.protocol("WM_DELETE_WINDOW", on_close)

    _poll_log()
    try:
        root.mainloop()
    finally:
        _cleanup_lock()


if __name__ == "__main__":
    main()
