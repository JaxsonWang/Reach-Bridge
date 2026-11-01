"""容器内真实浏览器验收：临时测试 CA 与 HTTPS 页面，不使用任何平台账号。"""

import argparse
import base64
import importlib.util
import json
import os
import signal
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "reach-bridge-smoke.test"
HTML = b"""<!doctype html><meta charset=utf-8><title>Reach-Bridge browser fixture</title>
<style>body{margin:0;background:#f4f7fa;font:20px sans-serif;height:2400px}
input{position:absolute;left:32px;top:80px;width:420px;height:48px;font:20px sans-serif}
#result{position:absolute;top:160px;left:32px}</style>
<h1>Reach-Bridge browser fixture</h1><input id=entry aria-label=fixture>
<p id=result>ready</p><script>
document.querySelector('#entry').addEventListener('keydown',e=>{
if(e.key==='Enter')document.querySelector('#result').textContent='submitted';});
</script>"""


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Host") != HOST:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, _format, *args):
        pass


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def certificate(directory, home):
    """测试信任仅写进此一次性容器的 HOME，不修改系统或真实用户 CA。"""
    ca, ca_key = directory / "ca.crt", directory / "ca.key"
    cert, key, csr = directory / "server.crt", directory / "server.key", directory / "server.csr"
    extensions = directory / "extensions.cnf"
    extensions.write_text(
        f"subjectAltName=DNS:{HOST}\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=Reach-Bridge Smoke CA",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
    )
    run(
        "openssl",
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        f"/CN={HOST}",
        "-keyout",
        str(key),
        "-out",
        str(csr),
    )
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        "1",
        "-extfile",
        str(extensions),
        "-out",
        str(cert),
    )
    nss = home / ".pki" / "nssdb"
    nss.mkdir(parents=True, mode=0o700)
    run("certutil", "-N", "--empty-password", "-d", f"sql:{nss}")
    run(
        "certutil", "-A", "-d", f"sql:{nss}", "-n", "reach-bridge-smoke", "-t", "C,,", "-i", str(ca)
    )
    return cert, key


def evaluate(upstream, target_id, expression, *, navigating=False):
    value = upstream.cdp.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
        },
        target_id=target_id,
    )
    if not value and navigating:
        # 刷新会短暂撤销旧执行上下文；仅就绪等待允许下一次读取，最终仍有硬截止。
        return None
    if not value or value.get("exceptionDetails"):
        raise RuntimeError("受控页面 DOM 验证失败")
    return value["result"].get("value")


def stop_diagnostic_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def diagnose_pipe(chrome, extension):
    """复用原 pipe 启动函数，只将 Chromium stderr 从 DEVNULL 改为可观察输出。"""
    watcher = importlib.import_module("agent_reach.runtime.browser_watcher")
    original = subprocess.Popen

    def observed(argv, *args, **kwargs):
        if argv[0] == chrome:
            print("PIPE_ARGV=" + json.dumps(argv), flush=True)
            kwargs["stderr"] = sys.stderr
        return original(argv, *args, **kwargs)

    watcher.subprocess.Popen = observed
    process = connection = None
    try:
        process, connection = watcher._launch_with_pipe(chrome, "about:blank", headless=True)
        response = connection.call("Browser.getVersion", timeout=5)
        print("PIPE_BROWSER_VERSION=" + json.dumps(response), flush=True)
        if response and "result" in response and (extension / "manifest.json").is_file():
            loaded = connection.call("Extensions.loadUnpacked", {"path": str(extension)}, timeout=5)
            print("PIPE_EXTENSION_RESULT=" + json.dumps(loaded), flush=True)
        print("PIPE_EXIT_BEFORE_CLEANUP=" + str(process.poll()), flush=True)
    finally:
        if connection is not None:
            connection.close()
        if process is not None:
            stop_diagnostic_process(process)
        watcher.subprocess.Popen = original


def browser_diagnostics(upstream, directory):
    """失败诊断不是重试验收；无论诊断结果如何，调用方仍抛出原失败。"""
    chrome = "/usr/bin/chromium"
    for page in upstream.cdp._json("/json/list") or []:
        if page.get("type") != "page" or not page.get("url", "").startswith(f"https://{HOST}/"):
            continue
        target_id = page["id"]
        metrics = upstream.cdp.call("Page.getLayoutMetrics", target_id=target_id)
        window = upstream.cdp.call("Browser.getWindowForTarget", {"targetId": target_id})
        print("FRAME_LAYOUT=" + json.dumps(metrics), flush=True)
        print("FRAME_WINDOW=" + json.dumps(window), flush=True)
        captured = upstream.cdp.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
            },
            target_id=target_id,
        )
        if captured and isinstance(captured.get("data"), str):
            png = base64.b64decode(captured["data"], validate=True)
            signature = len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n"
            print(
                "FRAME_PNG="
                + json.dumps(
                    {
                        "png_signature": signature,
                        "bytes": len(png),
                        "ihdr_size": struct.unpack(">II", png[16:24]) if signature else None,
                    }
                ),
                flush=True,
            )
    for name in (
        "/proc/self/attr/current",
        "/proc/sys/user/max_user_namespaces",
        "/proc/sys/kernel/unprivileged_userns_clone",
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
    ):
        path = Path(name)
        try:
            value = path.read_text(encoding="utf-8").strip()[:200]
        except OSError as error:
            value = "unavailable:" + type(error).__name__
        print("NAMESPACE_POLICY=" + json.dumps({"path": name, "value": value}), flush=True)
    extension = upstream.browser.extension_dir()
    manifest = extension / "manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        print(
            "EXTENSION_VERSION="
            + json.dumps({key: data.get(key) for key in ("version", "manifest_version")}),
            flush=True,
        )
    for executable in (chrome, "/opt/node/bin/node", upstream.tools.resolve("opencli")):
        if executable:
            result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
            print(
                "VERSION="
                + json.dumps(
                    {
                        "executable": executable,
                        "exit": result.returncode,
                        "stdout": result.stdout[-1000:],
                        "stderr": result.stderr[-1000:],
                    }
                ),
                flush=True,
            )
    profile = directory / "diagnostic-minimal-profile"
    argv = upstream.browser.chrome_argv(chrome, "about:blank", pipe=False, headless=True)
    argv = [
        f"--user-data-dir={profile}"
        if arg.startswith("--user-data-dir=")
        else "--remote-debugging-port=0"
        if arg.startswith("--remote-debugging-port=")
        else arg
        for arg in argv
    ]
    argv.append("--enable-logging=stderr")
    print("MINIMAL_ARGV=" + json.dumps(argv), flush=True)
    log = directory / "diagnostic-minimal.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True
        )
        try:
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if (profile / "DevToolsActivePort").is_file():
                    break
                time.sleep(0.1)
            print(
                "MINIMAL_RESULT="
                + json.dumps(
                    {
                        "exit_before_cleanup": process.poll(),
                        "devtools_ready": (profile / "DevToolsActivePort").is_file(),
                    }
                ),
                flush=True,
            )
        finally:
            stop_diagnostic_process(process)
    print(
        "MINIMAL_STDERR=" + log.read_text(encoding="utf-8", errors="replace")[-12000:], flush=True
    )
    # 分离 HOME，避免原启动留下的 profile 锁影响 pipe/扩展对照；不使用任何账号。
    diagnostic_home = directory / "diagnostic-pipe-home"
    diagnostic_home.mkdir(mode=0o700)
    result = subprocess.run(
        [sys.executable, __file__, "--diagnose-pipe", "--extension", str(extension)],
        env={**os.environ, "HOME": str(diagnostic_home)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    print("PIPE_DIAGNOSTIC_EXIT=" + str(result.returncode), flush=True)
    print(result.stdout[-12000:], flush=True)
    print(result.stderr[-12000:], file=sys.stderr, flush=True)


def exercise(helper, upstream, channel):
    print("准备系统 Chromium、OpenCLI 与扩展", flush=True)
    helper.execute("browser_prepare", channel.name, {}, upstream)
    state = upstream.browser._load_state()
    assert (
        state["backend"] == "chrome"
        and state["executable"] == os.environ["REACH_BRIDGE_BROWSER_EXECUTABLE"]
    )
    assert upstream.browser.status()["opencli_connected"] is True
    print("打开受控 HTTPS 登录画布", flush=True)
    opened = helper.execute("browser_login", channel.name, {}, upstream)
    target_id = opened["target_id"]
    ready = upstream.browser.wait_for(
        lambda: evaluate(
            upstream, target_id, "!!document.querySelector('#entry')", navigating=True
        ),
        15,
    )
    assert ready, "受控 HTTPS 页面未就绪（包括证书信任问题）"
    frame = helper.execute("browser_frame", channel.name, {"target_id": target_id}, upstream)
    png = base64.b64decode(frame["image"], validate=True)
    print(
        "FRAME_ACCEPTANCE="
        + json.dumps(
            {
                "png_signature": png[:8] == b"\x89PNG\r\n\x1a\n",
                "bytes": len(png),
                "ihdr_size": struct.unpack(">II", png[16:24]),
                "reported_size": [frame["width"], frame["height"]],
            }
        ),
        flush=True,
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (1280, 800)
    assert frame["url"] == f"https://{HOST}/login"

    def action(name, **values):
        helper.execute(
            "browser_input",
            channel.name,
            {"target_id": target_id, "action": name, **values},
            upstream,
        )

    action("click", x=150, y=105)
    action("type", text="browser-smoke-initial")
    action("key", key="Control+A")
    action("type", text="browser-smoke-confirmed")
    action("key", key="Enter")
    result = evaluate(
        upstream,
        target_id,
        "JSON.stringify({value:document.querySelector('#entry').value,"
        "result:document.querySelector('#result').textContent})",
    )
    assert json.loads(result) == {"value": "browser-smoke-confirmed", "result": "submitted"}
    action("scroll", delta=400)
    assert upstream.browser.wait_for(lambda: evaluate(upstream, target_id, "window.scrollY") > 0, 5)
    action("reload")
    assert upstream.browser.wait_for(
        lambda: evaluate(
            upstream,
            target_id,
            "!!document.querySelector('#entry') && document.querySelector('#entry').value === ''",
            navigating=True,
        ),
        10,
    )
    sandbox = os.environ.get("REACH_BRIDGE_BROWSER_SANDBOX", "true")
    profile_arg = "--user-data-dir=" + str(upstream.browser.profile_dir())
    commands = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().decode().split("\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if profile_arg in command and not any(arg.startswith("--type=") for arg in command):
            commands.append(command)
    assert commands, "未找到本次浏览器主进程"
    assert all(("--no-sandbox" in command) == (sandbox == "false") for command in commands)
    print("BROWSER_SANDBOX=" + sandbox + "; 已核对实际 Chromium 进程参数", flush=True)
    print("已验证：扩展连接、HTTPS 页面、1280x800 PNG、点击、输入、按键、滚动、刷新", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--helper", type=Path, default=Path("/app/src/reach_bridge/upstream_admin.py")
    )
    parser.add_argument("--diagnose-pipe", action="store_true")
    parser.add_argument("--extension", type=Path)
    args = parser.parse_args()
    if os.getuid() == 0:
        raise RuntimeError("浏览器验收必须以非 root 用户运行")
    if args.diagnose_pipe:
        diagnose_pipe("/usr/bin/chromium", args.extension)
        return
    # 永不读取已有 HOME；所有浏览器、OpenCLI、测试证书均在这个临时目录中。
    with tempfile.TemporaryDirectory(prefix="reach-bridge-browser-smoke-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        os.environ["HOME"] = str(home)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("XDG_CACHE_HOME", None)
        spec = importlib.util.spec_from_file_location(
            "reach_bridge_browser_smoke_helper", args.helper
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        upstream = helper.load_upstream()
        # 原 getter 返回全局 ALL_CHANNELS；fixture 只扩展 helper 自己的列表。
        registry = importlib.import_module("agent_reach.channels")
        base = importlib.import_module("agent_reach.channels.base").Channel

        class SmokeChannel(base):
            name = "browser-smoke"
            domains = (HOST,)
            login_url = f"https://{HOST}/login"

            def can_handle(self, url):
                return url.startswith(f"https://{HOST}/")

        channel = SmokeChannel()
        upstream.channels = [*upstream.channels, channel]
        assert (
            channel not in registry.get_all_channels()
            and registry.get_channel(channel.name) is None
        )
        cert, key = certificate(home, home)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = ThreadingHTTPServer(("127.0.0.1", 443), Page)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise(helper, upstream, channel)
        except Exception:
            # 此 HOME 无账号；打印上游浏览器日志用于定位 sandbox/扩展的真实失败。
            log = upstream.browser.watcher_log_path()
            if log.is_file():
                print("浏览器 fixture 日志：", file=sys.stderr)
                print(log.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
            try:
                browser_diagnostics(upstream, home)
            except Exception as diagnosis_error:
                print("浏览器诊断未完成：" + type(diagnosis_error).__name__, file=sys.stderr)
            raise
        finally:
            try:
                if upstream.browser.is_running():
                    upstream.browser.stop()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    main()
