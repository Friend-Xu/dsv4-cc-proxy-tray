# dsv4-cc-proxy CLI 入口
import argparse
import os
import signal
import sys
import time
import tempfile
import subprocess
import uvicorn
from dsv4_cc_proxy._version import VERSION
from dsv4_cc_proxy.proxy import DUMP_DIR, HOST, LOG_LEVEL, PORT

# 使用系统默认的临时目录，解决 Windows 下 /tmp 路径不适用的问题
PIDFILE_DEFAULT = os.path.join(tempfile.gettempdir(), "dsv4-cc-proxy.pid")


def _is_process_running(pid):
    """跨平台的进程存在性检查。"""
    if sys.platform == "win32":
        # Windows: 使用 tasklist 命令查询
        try:
            output = subprocess.check_output(
                f'tasklist /FI "PID eq {pid}" /FO CSV',
                shell=True,
                stderr=subprocess.DEVNULL
            ).decode('gbk', errors='ignore')
            # 如果能找到对应 PID，则进程存在
            return f'"{pid}"' in output
        except Exception:
            return False
    else:
        # Unix-like 系统 (Linux, macOS): 使用信号0
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _stop(pidfile: str):
    """停止代理：读取 PID 文件 → SIGTERM → 等待 → SIGKILL（超时则强制杀）。"""
    if not os.path.exists(pidfile):
        print(f"Proxy not running (PID file not found: {pidfile})")
        sys.exit(1)

    # 1. 读取 PID
    with open(pidfile) as f:
        pid = int(f.read().strip())
    print(f"Stopping dsv4-cc-proxy (PID {pid})...")

    # 2. 发送 SIGTERM (Windows下会直接终止进程)
    try:
        if sys.platform == "win32":
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Process not found, cleaning up PID file")
        os.unlink(pidfile)
        return

    # 3. 等待进程结束 (仅Linux/macOS需要)
    if sys.platform != "win32":
        for _ in range(10):
            time.sleep(0.5)
            if not _is_process_running(pid):
                print("Proxy stopped gracefully")
                try:
                    os.unlink(pidfile)
                except FileNotFoundError:
                    pass
                return

        # 4. 超时则强制杀掉 (仅Linux/macOS)
        print("Graceful shutdown timed out, sending SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # 5. 清理 PID 文件
    try:
        os.unlink(pidfile)
    except FileNotFoundError:
        pass
    print("Proxy stopped")


def main():
    parser = argparse.ArgumentParser(description="DeepSeek Thinking Proxy")
    parser.add_argument("--stop", action="store_true", help="Stop running proxy")
    parser.add_argument("--pidfile", default=PIDFILE_DEFAULT, help=f"PID file path (default: {PIDFILE_DEFAULT})", )
    args = parser.parse_args()

    if args.stop:
        _stop(args.pidfile)
        return

    pidfile = args.pidfile

    # 检查是否已有实例在运行
    if os.path.exists(pidfile):
        with open(pidfile) as f:
            try:
                pid = int(f.read().strip())
                # ✅ 修复点1：使用跨平台的进程检查函数
                if _is_process_running(pid):
                    print(f"Proxy already running (PID {pid}), use --stop first")
                    sys.exit(1)
                else:
                    # 如果进程不存在，说明是残留文件，直接清理
                    os.unlink(pidfile)
            except (OSError, ValueError):
                os.unlink(pidfile)

    # 写入 PID 文件
    # 注意：这里使用 'x' 模式打开文件，可以防止竞态条件的发生。
    # 如果文件已存在（另一个进程刚刚创建），会直接报错退出。
    try:
        with open(pidfile, "x") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        print(f"Error: PID file {pidfile} already exists. Is another instance starting?")
        sys.exit(1)

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
        try:
            os.unlink(pidfile)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()