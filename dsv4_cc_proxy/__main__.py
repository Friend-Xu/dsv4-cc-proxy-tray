# dsv4-cc-proxy CLI 入口 (Windows 兼容修复版)
import argparse
import atexit
import os
import signal
import subprocess
import sys
import tempfile
import time

import uvicorn

from dsv4_cc_proxy._version import VERSION
from dsv4_cc_proxy.proxy import DUMP_DIR, HOST, LOG_LEVEL, PORT

# 使用系统临时目录，自动适配 Windows / Linux / macOS
PIDFILE_DEFAULT = os.path.join(tempfile.gettempdir(), "dsv4-cc-proxy.pid")


def _is_process_running(pid):
    """跨平台检查进程是否存活"""
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(
                f'tasklist /FI "PID eq {pid}" /FO CSV', shell=True, stderr=subprocess.DEVNULL
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


def _safe_unlink(path, retries=5):
    """安全删除文件，解决 Windows 下文件可能被瞬间锁定的问题"""
    for _ in range(retries):
        try:
            os.unlink(path)
            return
        except PermissionError:
            time.sleep(0.5)
        except FileNotFoundError:
            return


def _stop(pidfile: str):
    """停止代理实例（跨平台）"""
    if not os.path.exists(pidfile):
        print(f"Proxy not running (PID file not found: {pidfile})")
        sys.exit(1)

    with open(pidfile) as f:
        pid = int(f.read().strip())

    print(f"Stopping dsv4-cc-proxy (PID {pid})...")

    if sys.platform == "win32":
        # Windows: 使用 taskkill 终止进程树
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        _safe_unlink(pidfile)
        print("Proxy stopped (forced).")
        return

    # Unix: 先发送 SIGTERM，超时后 SIGKILL
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Process not found, cleaning up PID file.")
        _safe_unlink(pidfile)
        return

    for _ in range(10):
        time.sleep(0.5)
        if not _is_process_running(pid):
            print("Proxy stopped gracefully.")
            _safe_unlink(pidfile)
            return

    print("Graceful shutdown timed out, sending SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _safe_unlink(pidfile)
    print("Proxy stopped (forced).")


def main():
    parser = argparse.ArgumentParser(description="DeepSeek Thinking Proxy")
    parser.add_argument("--stop", action="store_true", help="Stop running proxy")
    parser.add_argument("--pidfile", default=PIDFILE_DEFAULT, help=f"PID file path (default: {PIDFILE_DEFAULT})")
    args = parser.parse_args()

    pidfile = args.pidfile

    if args.stop:
        _stop(pidfile)
        return

    # ---------- 启动前的清理与检查 ----------
    # 先尝试删除可能残留的死文件（如上次异常退出未清理）
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                old_pid = int(f.read().strip())
            if _is_process_running(old_pid):
                print(f"Proxy already running (PID {old_pid}). Use --stop first.")
                sys.exit(1)
            else:
                # 进程已死，清理残留文件
                print(f"Removing stale PID file (PID {old_pid} no longer exists)...")
                _safe_unlink(pidfile)
        except (OSError, ValueError):
            # 文件损坏，直接删掉
            _safe_unlink(pidfile)

    # 写入 PID（使用 'x' 模式避免竞态）
    try:
        with open(pidfile, "x") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        print(f"Error: PID file {pidfile} already exists. Is another instance starting?")
        sys.exit(1)

    # 注册退出时自动清理，确保无论如何退出都能删除 PID 文件
    atexit.register(_safe_unlink, pidfile)

    print(f"DeepSeek Thinking Proxy v{VERSION} → {HOST}:{PORT} (PID {os.getpid()})")
    if DUMP_DIR:
        print(f"⚠ DUMP mode: {DUMP_DIR}")

    try:
        uvicorn.run(
            "dsv4_cc_proxy.proxy:create_app",
            host=HOST,
            port=PORT,
            log_level=LOG_LEVEL,
            factory=True,
        )
    finally:
        # 正常退出时再次确保清理（atexit 已经注册，这里做双重保障）
        _safe_unlink(pidfile)


if __name__ == "__main__":
    main()
