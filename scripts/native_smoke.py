"""独立容器原生验收：真实管理 HTTP/CLI，无平台账号，不启动显示服务或浏览器。"""

import http.cookiejar
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ORIGIN = "http://127.0.0.1:8080"
AUDIT = Path("/tmp/native-cli-audit.jsonl")
WRAPPER = Path("/tmp/native-agent-reach")
PROFILE = Path(os.environ.get("HOME", "/data/home")) / ".agent-reach/browser/profile"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def serve():
    # 仅验收使用的透明记录器；参数和 stdin 原样交给镜像内真正的上游 CLI。
    WRAPPER.write_text(
        '#!/bin/sh\nexec /usr/local/bin/python /tmp/native_smoke.py cli "$@"\n',
        encoding="utf-8",
    )
    WRAPPER.chmod(0o700)
    AUDIT.write_text("", encoding="utf-8")
    os.environ["REACH_BRIDGE_EXECUTABLE"] = str(WRAPPER)
    os.environ["REACH_BRIDGE_UPSTREAM_PYTHON"] = "/opt/uv-tools/agent-reach/bin/python"
    os.execvp("reach-bridge", ["reach-bridge"])


def cli():
    arguments = sys.argv[2:]
    # configure 的值只走 stdin；审计不记录 stdin，也不记录完整参数。
    with AUDIT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(arguments[:2]) + "\n")
    os.execv("/opt/uv-bin/agent-reach", ["agent-reach", *arguments])


def browser_absent():
    require(not PROFILE.exists(), "原生模式创建了专用 Chrome profile")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            name = (process / "comm").read_text().strip().lower()
        except FileNotFoundError:
            continue  # 进程在枚举和读取之间正常退出。
        require(
            not any(item in name for item in ("chromium", "chrome", "xvfb")),
            "原生模式出现浏览器或 Xvfb 进程",
        )


class Client:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.csrf = None

    def request(self, method, path, body=None, *, status=200, query=False):
        browser_absent()
        headers = {"Origin": ORIGIN}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if query:
            headers["Authorization"] = "Bearer " + os.environ["REACH_BRIDGE_TOKEN"]
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(ORIGIN + path, data, headers, method=method)
        try:
            response = self.http.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            require(response.status == status, f"HTTP {method} {path}: 状态 {response.status}")
            result = json.load(response)
        browser_absent()
        return result

    def job(self, path, body):
        started = self.request("POST", path, body)
        deadline = time.monotonic() + 330
        while time.monotonic() < deadline:
            rows = self.request("GET", "/admin/api/jobs")["jobs"]
            job = next(row for row in rows if row["id"] == started["id"])
            if job["state"] == "succeeded":
                return job
            require(
                job["state"] not in {"failed", "cancelled"},
                f"管理任务 {path} 未完成：{job['state']} / {job.get('code', 'UNKNOWN')}",
            )
            time.sleep(0.25)
        raise RuntimeError(f"管理任务 {path} 超过 330 秒硬截止")


def audit():
    return [json.loads(line) for line in AUDIT.read_text().splitlines()]


def check():
    # 独立于 HTTP/上游安装超时，保证整个验收不会无限等待。
    def deadline(_signum, _frame):
        raise RuntimeError("原生验收超过 750 秒硬截止")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(750)
    violations = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                browser_absent()
            except Exception:
                violations.append("原生模式进程/profile 持续检查失败")
                return
            stop.wait(0.05)

    observer = threading.Thread(target=watch, daemon=True)
    observer.start()
    try:
        client = Client()
        ready = time.monotonic() + 30
        while True:
            try:
                client.request("GET", "/readyz")
                break
            except urllib.error.URLError:
                if time.monotonic() >= ready:
                    raise RuntimeError("原生服务未在 30 秒内启动") from None
                time.sleep(0.25)
        bootstrap = client.request("GET", "/admin/bootstrap")
        require(bootstrap["browser_enabled"] is False, "原生模式配置未生效")
        require(bootstrap["insecure_http"] is True, "测试 HTTP 未显式启用")
        session = client.request(
            "POST", "/admin/login", {"token": os.environ["REACH_BRIDGE_ADMIN_TOKEN"]}
        )
        client.csrf = session["csrf"]
        credentials = client.request("GET", "/admin/api/credentials")
        capabilities = credentials["capabilities"]
        require(capabilities["browser_enabled"] is False, "凭据能力缺少原生模式标记")
        channels = capabilities["channels"]
        require(all(not row["login"] for row in channels.values()), "原生模式宣称浏览器登录可用")
        for platform in ("bilibili", "youtube"):
            require(channels[platform]["cookie_import"], f"{platform} 原生 Cookie 不可用")
        require(not channels["xiaohongshu"]["cookie_import"], "小红书 Cookie 错误开放")
        before = audit()
        jobs_before = client.request("GET", "/admin/api/jobs")["jobs"]
        for path, body, query in (
            ("/v1/actions/xiaohongshu/search", {"params": {"query": "fixture"}}, True),
            ("/admin/api/login", {"platform": "bilibili"}, False),
            (
                "/admin/api/cookies",
                {"platform": "xiaohongshu", "cookies": "web_session=not-a-real-cookie"},
                False,
            ),
        ):
            rejected = client.request("POST", path, body, status=503, query=query)
            require(rejected["error"]["code"] == "BROWSER_DISABLED", "浏览器拒绝码不符")
        require(audit() == before, "被拒绝的浏览器操作执行了上游 CLI")
        require(
            client.request("GET", "/admin/api/jobs")["jobs"] == jobs_before, "拒绝操作创建了任务"
        )
        print("NATIVE: 能力与拒绝路径通过，未执行 CLI", flush=True)
        for platform in ("bilibili", "youtube"):
            installed = client.job("/admin/api/install", {"platform": platform})["result"]
            require(
                installed.get("added") is True and installed.get("channel") == platform,
                f"{platform} 上游未确认安装完成",
            )
            require(["add", platform] in audit(), f"{platform} 未执行真实上游 add")
            print(f"NATIVE: 真实 add {platform} 完成", flush=True)
        client.job(
            "/admin/api/credentials",
            {"key": "github-token", "value": "native-smoke-not-a-real-github-token"},
        )
        state = client.request("GET", "/admin/api/credentials")["status"]
        require(state["keys"]["github-token"] is True, "测试 GitHub key 未保存")
        client.job("/admin/api/credentials", {"key": "github-token", "value": None})
        state = client.request("GET", "/admin/api/credentials")["status"]
        require(state["keys"]["github-token"] is False, "测试 GitHub key 未删除")
        browser_absent()
        require(not violations, "进程/profile 持续检查失败")
        print("NATIVE: 凭据增删完成；全程未发现专用 profile、Chromium 或 Xvfb", flush=True)
    finally:
        stop.set()
        observer.join(timeout=2)
        signal.alarm(0)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "serve":
        serve()
    elif mode == "cli":
        cli()
    elif mode == "check":
        check()
    else:
        raise SystemExit("仅支持 serve / cli / check")
