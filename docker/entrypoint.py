"""官方浏览器按需显示；仅已有自定义浏览器保留共享显示服务。"""

import os
import signal
import subprocess
import sys
import time


def prepare_display_policy():
    if os.environ.get("REACH_BRIDGE_BROWSER_ENABLED", "true").lower() != "true":
        return False
    launcher = "/app/.venv/bin/reach-bridge-chromium"
    configured = os.environ.get("REACH_BRIDGE_BROWSER_EXECUTABLE", launcher)
    # 只迁移旧官方选择，手动 CLI 与 WebUI 均无需先重新准备；自定义选择不变。
    selected = subprocess.check_output(
        [
            "/opt/uv-tools/agent-reach/bin/python",
            "-c",
            "import os; from agent_reach.runtime import browser; "
            "state = browser._load_state(); "
            "launcher = os.environ.get('REACH_BRIDGE_BROWSER_EXECUTABLE', ''); "
            "official = launcher == '/app/.venv/bin/reach-bridge-chromium'; "
            "old = state.get('backend') == 'chrome' "
            "and state.get('executable') == '/usr/bin/chromium'; "
            "browser.update_state(executable=launcher) if official and old else None; "
            "print(browser.chrome_binary() or '')",
        ],
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("REACH_BRIDGE_") or key == "REACH_BRIDGE_BROWSER_EXECUTABLE"
        },
    ).rstrip("\n")
    official = {"", "/usr/bin/chromium", launcher}
    return configured not in official or selected not in official


def main():
    command = sys.argv[1:]
    if not command:
        print("缺少容器启动命令", file=sys.stderr)
        return 2
    if not prepare_display_policy():
        # 默认与原生模式直接交接，避免常驻 Python 监督进程和 Xvfb。
        os.execvp(command[0], command)
    display = os.environ.get("DISPLAY", ":99")
    if not display.startswith(":") or not display[1:].isdigit():
        print("DISPLAY 须为容器内本地显示编号，例如 :99", file=sys.stderr)
        return 2
    os.environ["DISPLAY"] = display
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
            try:
                ready = subprocess.run(
                    ["xdpyinfo", "-display", display],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                # 现有自定义浏览器仍使用原来的 10 秒显示就绪期限。
                continue
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
