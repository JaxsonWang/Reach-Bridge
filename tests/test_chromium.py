"""显式兼容模式的启动、配置与子进程边界。"""

import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from reach_bridge import chromium
from reach_bridge.app import create_app
from reach_bridge.config import Settings
from reach_bridge.runner import Runner


@pytest.mark.parametrize("mode", [None, "true", "TRUE", "false", "FALSE"])
@pytest.mark.parametrize("headless", [None, "--headless", "--headless=new"])
def test_launcher_preserves_pid_and_uses_display_only_when_headed(monkeypatch, mode, headless):
    monkeypatch.setenv("DISPLAY", ":original")
    monkeypatch.delenv("REACH_BRIDGE_BROWSER_SANDBOX", raising=False)
    if mode is not None:
        monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", mode)
    monkeypatch.setattr(chromium.os, "getuid", lambda: 1026)
    arguments = ["--remote-debugging-pipe", "--user-data-dir=/tmp/profile", "about:blank"]
    if headless:
        arguments.insert(0, headless)
    monkeypatch.setattr(sys, "argv", ["reach-bridge-chromium", *arguments])
    called = []
    displays = []

    def start_display():
        displays.append("started")
        return "display-process", ":101"

    monkeypatch.setattr(chromium, "start_display", start_display)
    monkeypatch.setattr(chromium, "stop_display", lambda process: displays.append(process))
    monkeypatch.setattr(chromium.os, "execv", lambda *args: called.append(args))
    chromium.main()
    extra = ["--no-sandbox"] if mode and mode.lower() == "false" else []
    assert called == [("/usr/bin/chromium", ["/usr/bin/chromium", *extra, *arguments])]
    assert displays == ([] if headless else ["started", "display-process"])
    if not headless:
        assert os.environ["DISPLAY"] == ":101"
    else:
        assert os.environ["DISPLAY"] == ":original"


@pytest.mark.parametrize("state", ["bound", "failed", "parent_gone"])
def test_display_parent_binding_is_required(monkeypatch, state):
    calls = []

    class Library:
        def prctl(self, *args):
            calls.append(args)
            return -1 if state == "failed" else 0

    monkeypatch.setattr(chromium.ctypes, "CDLL", lambda *_a, **_kw: Library())
    monkeypatch.setattr(chromium.os, "getppid", lambda: 123 if state != "parent_gone" else 1)

    class Exited(Exception):
        pass

    def exit_child(code):
        assert code == 1
        raise Exited

    monkeypatch.setattr(chromium.os, "_exit", exit_child)
    if state == "failed":
        with pytest.raises(OSError, match="生命周期"):
            chromium.bind_display_to_parent(123)
    elif state == "parent_gone":
        with pytest.raises(Exited):
            chromium.bind_display_to_parent(123)
    else:
        chromium.bind_display_to_parent(123)
    assert calls == [(1, chromium.signal.SIGTERM, 0, 0, 0)]


@pytest.mark.parametrize("result", [b"101\n", b"", b"invalid\n", None])
def test_display_readiness_and_failure_cleanup(monkeypatch, result):
    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

    process = Process()
    seen = []

    def spawn(args, **kwargs):
        seen.append((args, kwargs))
        fd = int(args[2])
        assert kwargs["pass_fds"] == (fd,)
        assert "-nolisten" in args and "tcp" in args
        if result is not None:
            os.write(fd, result)
        return process

    monkeypatch.setattr(chromium.subprocess, "Popen", spawn)
    if result is None:
        monkeypatch.setattr(chromium.select, "select", lambda *_: ([], [], []))
    if result == b"101\n":
        assert chromium.start_display() == (process, ":101")
        assert not process.stopped
    else:
        with pytest.raises(RuntimeError, match="Xvfb"):
            chromium.start_display()
        assert process.stopped
    with pytest.raises(OSError):
        os.fstat(seen[0][1]["pass_fds"][0])


def test_display_is_reaped_if_chromium_exec_fails(monkeypatch):
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", "true")
    monkeypatch.setattr(sys, "argv", ["reach-bridge-chromium", "about:blank"])
    monkeypatch.setattr(chromium, "start_display", lambda: ("display", ":101"))
    stopped = []
    monkeypatch.setattr(chromium, "stop_display", stopped.append)

    def fail(*_args):
        raise FileNotFoundError("Chromium")

    monkeypatch.setattr(chromium.os, "execv", fail)
    with pytest.raises(FileNotFoundError):
        chromium.main()
    assert stopped == ["display"]


def test_stuck_display_is_killed_and_reaped():
    class Process:
        calls = []

        def poll(self):
            return None

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def wait(self, timeout=None):
            self.calls.append(("wait", timeout))
            if timeout:
                raise subprocess.TimeoutExpired("Xvfb", timeout)

    process = Process()
    chromium.stop_display(process)
    assert process.calls == ["terminate", ("wait", 5), "kill", ("wait", None)]


@pytest.mark.parametrize("mode", ["", "0", "off", "flase", " false "])
def test_invalid_mode_is_visible_and_never_launches(monkeypatch, capsys, mode):
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", mode)
    monkeypatch.setattr(chromium.os, "execv", lambda *_: pytest.fail("不能启动浏览器"))
    assert chromium.main() == 2
    assert "只能为 true 或 false" in capsys.readouterr().err
    with pytest.raises(ValueError, match="只能为 true 或 false"):
        Settings.from_env()


def test_compatibility_rejects_root(monkeypatch, capsys):
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", "false")
    monkeypatch.setattr(chromium.os, "getuid", lambda: 0)
    monkeypatch.setattr(chromium.os, "execv", lambda *_: pytest.fail("不能以 root 启动"))
    assert chromium.main() == 2
    assert "非 root" in capsys.readouterr().err


def test_sandbox_setting_defaults_to_enabled(settings, monkeypatch):
    monkeypatch.delenv("REACH_BRIDGE_BROWSER_SANDBOX", raising=False)
    assert settings.browser_sandbox is True
    assert Settings.from_env().browser_sandbox is True
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", "false")
    assert Settings.from_env().browser_sandbox is False
    with pytest.raises(ValueError, match="布尔值"):
        replace(settings, browser_sandbox="false")


@pytest.mark.parametrize("sandbox", [True, False])
async def test_runner_propagates_policy_without_secrets(settings, tmp_path, monkeypatch, sandbox):
    script = tmp_path / "environment.py"
    script.write_text(
        "import json,os; print(json.dumps({'schema_version':1,'ok':True,'result':"
        "{k:v for k,v in os.environ.items() if k.startswith('REACH_BRIDGE_')}}))"
    )
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_EXECUTABLE", "/app/.venv/bin/reach-bridge-chromium")
    monkeypatch.setenv("REACH_BRIDGE_ADMIN_TOKEN", "private-admin")
    monkeypatch.setenv("REACH_BRIDGE_TOKEN", "private-query")
    # Settings 是服务的权威配置；不得沿用进程环境中另一个 sandbox 选择。
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", "false" if sandbox else "true")
    runner = Runner(replace(settings, browser_sandbox=sandbox))
    try:
        result = await runner.run([str(script)], executable=sys.executable)
    finally:
        await runner.close()
    expected = {"REACH_BRIDGE_BROWSER_EXECUTABLE": "/app/.venv/bin/reach-bridge-chromium"}
    if not sandbox:
        expected["REACH_BRIDGE_BROWSER_SANDBOX"] = "false"
    assert result["result"] == expected


@pytest.mark.parametrize("sandbox,browser", [(True, True), (False, True), (False, False)])
def test_api_reports_sandbox_independently_of_browser_enabled(settings, sandbox, browser):
    config = replace(settings, browser_sandbox=sandbox, browser_enabled=browser)
    with TestClient(create_app(config)) as client:
        bootstrap = client.get("/admin/bootstrap").json()
        assert bootstrap["browser_sandbox"] is sandbox
        assert bootstrap["browser_enabled"] is browser
        headers = {"Authorization": "Bearer " + settings.token}
        catalog = client.get("/v1/channels", headers=headers).json()
        assert catalog["browser_sandbox"] is sandbox
        assert catalog["browser_enabled"] is browser
        assert catalog["channels"][0]["actions"] == ["search", "read"]
        assert "Chromium sandbox 已关闭" in client.get("/").text
        assert json.dumps(settings.token) not in client.get("/").text
