"""容器内 Chromium 的统一启动入口，兼容模式只改变内部 sandbox。"""

import os
import sys


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
    # exec 保留上游 pipe/CDP 文件描述符与 PID，信号和回收仍归原生命周期管理。
    os.execv(args[0], args)


if __name__ == "__main__":
    sys.exit(main())
