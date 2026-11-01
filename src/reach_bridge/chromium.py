"""容器内 Chromium 启动入口：仅可见浏览器按需使用私有 Xvfb。"""

import ctypes
import os
import select
import signal
import subprocess
import sys


def bind_display_to_parent(parent_pid):
    """只在单线程 launcher 的 Xvfb 子进程中执行，父 Chromium 退出即清理显示服务。"""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "无法绑定 Xvfb 生命周期")
    if os.getppid() != parent_pid:
        os._exit(1)


def stop_display(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def start_display():
    # -displayfd 由 Xvfb 选择空闲编号并在可连接时报告，避免外部轮询与编号竞争。
    read_fd, write_fd = os.pipe()
    process = None
    parent_pid = os.getpid()
    try:
        process = subprocess.Popen(
            [
                "Xvfb",
                "-displayfd",
                str(write_fd),
                "-screen",
                "0",
                "1280x800x24",
                "-nolisten",
                "tcp",
                "-noreset",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # 只传就绪管道；Xvfb 不得持有上游 CDP 的 FD 3/4。
            pass_fds=(write_fd,),
            preexec_fn=lambda: bind_display_to_parent(parent_pid),
        )
        os.close(write_fd)
        write_fd = None
        if not select.select([read_fd], [], [], 10)[0]:
            raise RuntimeError("Xvfb 未在 10 秒内就绪")
        number = os.read(read_fd, 32).strip()
        if not number.isdigit():
            raise RuntimeError("Xvfb 启动失败，请检查上面的显示服务错误")
        return process, ":" + number.decode("ascii")
    except BaseException:
        if process is not None:
            stop_display(process)
        raise
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def main():
    sandbox = os.environ.get("REACH_BRIDGE_BROWSER_SANDBOX", "true").lower()
    if sandbox not in {"true", "false"}:
        print("REACH_BRIDGE_BROWSER_SANDBOX 只能为 true 或 false", file=sys.stderr)
        return 2
    args = ["/usr/bin/chromium", *sys.argv[1:]]
    if sandbox == "false":
        if os.getuid() == 0:
            print("NAS 兼容模式必须以非 root 用户运行", file=sys.stderr)
            return 2
        args.insert(1, "--no-sandbox")
    display = None
    try:
        if not any(arg == "--headless" or arg.startswith("--headless=") for arg in args[1:]):
            display, address = start_display()
            os.environ["DISPLAY"] = address
        # PID 和 FD 3/4 原样交给真实 Chromium，watcher 的强制回收语义不变。
        os.execv(args[0], args)
    finally:
        # 仅启动或 exec 失败会返回此处；exec 成功后由父死亡信号释放 Xvfb。
        if display is not None:
            stop_display(display)


if __name__ == "__main__":
    sys.exit(main())
