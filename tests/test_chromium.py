"""显式兼容模式的启动、配置与子进程边界。"""

import json
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from reach_bridge import chromium
from reach_bridge.app import create_app
from reach_bridge.config import Settings
from reach_bridge.runner import Runner


@pytest.mark.parametrize("mode", [None, "true", "TRUE", "false", "FALSE"])
def test_launcher_changes_only_sandbox_flag(monkeypatch, mode):
    monkeypatch.delenv("REACH_BRIDGE_BROWSER_SANDBOX", raising=False)
    if mode is not None:
        monkeypatch.setenv("REACH_BRIDGE_BROWSER_SANDBOX", mode)
    monkeypatch.setattr(chromium.os, "getuid", lambda: 1026)
    arguments = ["--remote-debugging-pipe", "--user-data-dir=/tmp/profile", "about:blank"]
    monkeypatch.setattr(sys, "argv", ["reach-bridge-chromium", *arguments])
    called = []
    monkeypatch.setattr(chromium.os, "execv", lambda *args: called.append(args))
    chromium.main()
    extra = ["--no-sandbox"] if mode and mode.lower() == "false" else []
    assert called == [("/usr/bin/chromium", ["/usr/bin/chromium", *extra, *arguments])]


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
