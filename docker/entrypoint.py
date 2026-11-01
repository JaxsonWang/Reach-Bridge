"""容器入口：维护私有 Xvfb，转发服务信号，不放宽 Chromium sandbox。"""

import os
import signal
import subprocess
import sys
import time


def main():
    command = sys.argv[1:]
    if not command:
        print("缺少容器启动命令", file=sys.stderr)
        return 2
    display = os.environ.get("DISPLAY", ":99")
    if not display.startswith(":") or not display[1:].isdigit():
        print("DISPLAY 须为容器内本地显示编号，例如 :99", file=sys.stderr)
        return 2
    child = None
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-noreset"],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not stopping and time.monotonic() < deadline:
            if xvfb.poll() is not None:
                print("Xvfb 启动失败，请检查上面的显示服务错误", file=sys.stderr)
                return 1
            ready = subprocess.run(
                ["xdpyinfo", "-display", display],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            if not stopping:
                print("Xvfb 未在 10 秒内就绪", file=sys.stderr)
            return 1
        if stopping:
            return 1
        child = subprocess.Popen(command, start_new_session=True)
        status = child.wait()
        return status if status >= 0 else 128 - status
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait()


if __name__ == "__main__":
    sys.exit(main())
