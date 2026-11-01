"""默认空闲启动不创建显示服务，已有自定义浏览器保留原显示环境。"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "reach_bridge_entrypoint", Path(__file__).parents[1] / "docker" / "entrypoint.py"
)
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


@pytest.mark.parametrize(
    "selected,configured,expected",
    [
        ("", "/app/.venv/bin/reach-bridge-chromium", False),
        ("/usr/bin/chromium", "/app/.venv/bin/reach-bridge-chromium", False),
        ("/app/.venv/bin/reach-bridge-chromium", "/app/.venv/bin/reach-bridge-chromium", False),
        ("/custom/browser", "/app/.venv/bin/reach-bridge-chromium", True),
        ("", "/custom/browser", True),
    ],
)
def test_display_selection_preserves_custom_browser(monkeypatch, selected, configured, expected):
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_ENABLED", "true")
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_EXECUTABLE", configured)
    monkeypatch.setattr(
        entrypoint.subprocess, "check_output", lambda *_args, **_kw: selected + "\n"
    )
    assert entrypoint.prepare_display_policy() is expected


def test_native_mode_does_not_inspect_or_start_browser(monkeypatch):
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_ENABLED", "false")
    monkeypatch.setattr(
        entrypoint.subprocess, "check_output", lambda *_a, **_kw: pytest.fail("不能检查浏览器")
    )
    assert entrypoint.prepare_display_policy() is False


@pytest.mark.parametrize("previous", ["/usr/bin/chromium", "/custom/browser"])
def test_startup_migrates_only_old_official_selection(monkeypatch, capsys, previous):
    launcher = "/app/.venv/bin/reach-bridge-chromium"
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_ENABLED", "true")
    monkeypatch.setenv("REACH_BRIDGE_BROWSER_EXECUTABLE", launcher)
    monkeypatch.setenv("REACH_BRIDGE_TOKEN", "private-query")
    monkeypatch.setenv("REACH_BRIDGE_ADMIN_TOKEN", "private-admin")
    scripts = []

    def inspect(args, **kwargs):
        assert kwargs["env"]["REACH_BRIDGE_BROWSER_EXECUTABLE"] == launcher
        assert "REACH_BRIDGE_TOKEN" not in kwargs["env"]
        assert "REACH_BRIDGE_ADMIN_TOKEN" not in kwargs["env"]
        scripts.append(args[2])
        return previous + "\n"

    monkeypatch.setattr(entrypoint.subprocess, "check_output", inspect)
    entrypoint.prepare_display_policy()
    state = {"backend": "chrome", "executable": previous, "existing": "retained"}
    browser = SimpleNamespace(
        _load_state=lambda: dict(state),
        update_state=lambda **changes: state.update(changes),
        chrome_binary=lambda: state["executable"],
    )
    package, runtime = ModuleType("agent_reach"), ModuleType("agent_reach.runtime")
    runtime.browser = browser
    monkeypatch.setitem(sys.modules, "agent_reach", package)
    monkeypatch.setitem(sys.modules, "agent_reach.runtime", runtime)
    exec(scripts[0], {})
    expected = launcher if previous == "/usr/bin/chromium" else previous
    assert state == {"backend": "chrome", "executable": expected, "existing": "retained"}
    assert capsys.readouterr().out.strip() == expected


def test_default_command_executes_without_supervisor_or_display(monkeypatch):
    monkeypatch.setattr(entrypoint.sys, "argv", ["entrypoint", "reach-bridge"])
    monkeypatch.setattr(entrypoint, "prepare_display_policy", lambda: False)
    monkeypatch.setattr(
        entrypoint.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("不能启动显示服务")
    )

    class Executed(Exception):
        pass

    def execute(command, args):
        assert command == "reach-bridge" and args == ["reach-bridge"]
        raise Executed

    monkeypatch.setattr(entrypoint.os, "execvp", execute)
    with pytest.raises(Executed):
        entrypoint.main()


def test_custom_display_probe_timeout_uses_existing_startup_deadline(monkeypatch):
    monkeypatch.setattr(entrypoint.sys, "argv", ["entrypoint", "reach-bridge"])
    monkeypatch.setattr(entrypoint, "prepare_display_policy", lambda: True)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(entrypoint.signal, "signal", lambda *_: None)

    class Process:
        pid = 12345

        def poll(self):
            return 0 if getattr(self, "finished", False) else None

        def wait(self, timeout=None):
            self.finished = True
            return 0

        def terminate(self):
            self.finished = True

    display, service = Process(), Process()
    processes = iter([display, service])
    monkeypatch.setattr(entrypoint.subprocess, "Popen", lambda *_a, **_kw: next(processes))
    probes = []

    def probe(args, **kwargs):
        probes.append(args)
        if len(probes) == 1:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(entrypoint.subprocess, "run", probe)
    assert entrypoint.main() == 0
    assert len(probes) == 2
    assert entrypoint.os.environ["DISPLAY"] == ":99"
    assert display.finished and service.finished
