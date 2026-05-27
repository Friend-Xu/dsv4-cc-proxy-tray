# dsv4-cc-proxy / GUI — tkinter 图形界面启动器
#
# 通过子进程 ``python -m dsv4_cc_proxy`` 启动代理，stdout 读取日志。
# 打包成 exe 时整个 dsv4_cc_proxy/ 通过 --add-data 打入，
# PyInstaller 自动解压到 sys._MEIPASS，sys.executable 即为内嵌 Python。

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# ── 版本（独立定义，不 import proxy 模块） ─────────────────
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
_CONFIG_PATH = Path.home() / ".dsv4-cc-proxy-gui.json"
_GUI_LOCKFILE = Path.home() / ".dsv4-cc-proxy-gui.lock"
_PROXY_PIDFILE = Path.home() / "dsv4-cc-proxy.pid"


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
    "ERROR":   "red",
    "CRITICAL": "darkred",
    "WARNING":  "orange",
    "INFO":     "black",
    "DEBUG":    "gray",
}

_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(\w+)\s+(.*)$")

# ── 跨平台进程检查 ──────────────────────────────────────────


def _is_process_running(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(
                f'tasklist /FI "PID eq {pid}" /FO CSV',
                shell=True, stderr=subprocess.DEVNULL,
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

# ── GUI ──────────────────────────────────────────────────────


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox

    # ── 单实例保护 ──
    if _GUI_LOCKFILE.exists():
        try:
            old_pid = int(_GUI_LOCKFILE.read_text().strip())
            if _is_process_running(old_pid):
                # tkinter 还未初始化，用 ctypes MessageBox 弹窗
                import ctypes
                ctypes.windll.user32.MessageBoxW(0,
                    "dsv4-cc-proxy GUI 已在运行中，请查看系统托盘或任务栏。",
                    "提示", 0x40)
                sys.exit(0)
            else:
                _GUI_LOCKFILE.unlink(missing_ok=True)
        except (ValueError, OSError):
            _GUI_LOCKFILE.unlink(missing_ok=True)

    _GUI_LOCKFILE.write_text(str(os.getpid()))

    saved = _load_config()

    # ── 状态 ──
    proc: subprocess.Popen | None = None
    log_queue: queue.Queue = queue.Queue()
    exit_event = threading.Event()

    def _parse_host_port(s: str) -> tuple[str, int]:
        s = s.strip()
        if ":" in s:
            host, port_str = s.rsplit(":", 1)
            return host, int(port_str)
        return s, 16889

    # ── 启动 / 停止 ──

    def start():
        nonlocal proc

        if proc is not None and proc.poll() is None:
            messagebox.showinfo("提示", "代理已在运行中")
            return

        # 检查 PID 文件，防止重复启动代理（可能由其他 GUI 实例启动）
        if _PROXY_PIDFILE.exists():
            try:
                pid = int(_PROXY_PIDFILE.read_text().strip())
                if _is_process_running(pid):
                    messagebox.showinfo("提示", f"代理已在运行中 (PID {pid})")
                    _update_status()
                    return
                else:
                    _PROXY_PIDFILE.unlink(missing_ok=True)
            except (ValueError, OSError):
                _PROXY_PIDFILE.unlink(missing_ok=True)

        upstream = upstream_var.get()
        listen_addr = listen_var.get()
        log_level = log_level_var.get()
        host, port = _parse_host_port(listen_addr)

        _save_config({
            "upstream": upstream,
            "listen": listen_addr,
            "log_level": log_level,
        })

        env = {
            **os.environ,
            "PROXY_UPSTREAM": upstream,
            "PROXY_HOST": host,
            "PROXY_PORT": str(port),
            "PROXY_LOG_LEVEL": log_level,
            "PYTHONIOENCODING": "utf-8",
        }

        _append_text(f"启动代理 v{VERSION}\n", "INFO")

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "dsv4_cc_proxy"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            _append_text(f"启动失败: {e}\n", "ERROR")
            return

        exit_event.clear()
        threading.Thread(target=_read_stdout, daemon=True).start()
        threading.Thread(target=_monitor_exit, daemon=True).start()
        _update_status()

    def stop():
        nonlocal proc
        if proc is None:
            messagebox.showinfo("提示", "代理未在运行")
            return
        exit_event.set()
        try:
            proc.terminate()
        except OSError:
            pass
        threading.Thread(target=_wait_stop, daemon=True).start()

    def _wait_stop():
        nonlocal proc
        if proc is None:
            return
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        proc = None
        _update_status()
        _append_text("代理已停止\n", "INFO")

    def _read_stdout():
        if proc is None or proc.stdout is None:
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                if exit_event.is_set():
                    break
                if line:
                    log_queue.put(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    def _monitor_exit():
        if proc is None:
            return
        try:
            proc.wait()
        except OSError:
            pass
        if not exit_event.is_set():
            log_queue.put("代理进程异常退出")
        _update_status()

    def _update_status():
        def _do():
            running = proc is not None and proc.poll() is None
            status_label.config(text="● 运行中" if running else "○ 已停止",
                               foreground="green" if running else "gray")
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
        _tags_inited = True

    def _flush_log():
        if not _log_buffer:
            return

        _init_tags()
        lines = []
        for ts, msg, color in _log_buffer:
            lines.append(ts)
            lines.append(msg + "\n")

        log_text.config(state="normal")
        log_text.insert("end", "".join(lines), ("ts",))
        # 回退为每行上色：在 insert 后对每行逐个打 tag（比逐行 insert 快很多）
        pos = log_text.index("end-1c linestart")
        for ts, msg, color in _log_buffer:
            if color != "black":
                start = log_text.index(f"{pos}+{len(ts)}c")
                end = log_text.index(f"{start}+{len(msg)}c")
                log_text.tag_add(color, start, end)
            pos = log_text.index(f"{pos}+{len(ts) + len(msg) + 1}c")

        _log_buffer.clear()

        # 行数限制：超过 MAX_LINES 时裁掉最老的
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
    root.title(f"dsv4-cc-proxy v{VERSION} GUI")
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
        row=2, column=1, sticky="w", pady=2)

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

    ttk.Label(btn_frame, text=f"上游: {_DEFAULT_UPSTREAM}").pack(side="left", padx=(0, 0))

    # ── 日志区 ──

    log_frame = ttk.LabelFrame(root, text="日志", padding=4)
    log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 4))

    log_text = tk.Text(log_frame, state="disabled", wrap="word",
                       font=("Consolas", 9), relief="sunken", borderwidth=1)
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
        if proc is not None and proc.poll() is None:
            if messagebox.askokcancel("退出", "代理正在运行，确定退出并停止代理？"):
                stop()
                root.after(500, root.destroy)
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
