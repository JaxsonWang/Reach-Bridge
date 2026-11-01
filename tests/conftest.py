"""真实子进程夹具；不访问任何平台或个人配置。"""

import json
import sys
from pathlib import Path

import pytest

from reach_bridge.config import Settings

TOKEN = "test-only-placeholder-token-not-a-real-secret"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cli = tmp_path / "agent-reach"
    cli.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ["version"]:
    print(json.dumps({"schema_version":1,"ok":True,"result":{"version":"1.6.1"}}))
elif args == ["channels"]:
    search = {"name":"search","params":[{"name":"query","required":True},
              {"name":"limit","required":False}]}
    read = {"name":"read","params":[{"name":"target","required":True}]}
    print(json.dumps({"schema_version":1,"ok":True,"channels":[
        {"name":"bilibili","description":"测试平台","actions":[search,read]}]}))
else:
    query = args[-1]
    if query == "slow": time.sleep(30)
    if query == "large":
        sys.stdout.write("x" * 3000000); sys.exit(0)
    if query == "stderr":
        sys.stderr.write("private-fixture" * 200000); sys.exit(1)
    if query == "invalid":
        print("private-invalid-json"); sys.exit(1)
    if query == "spawn":
        child = subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
        def reap(signum, frame):
            child.wait(timeout=1); sys.exit(0)
        signal.signal(signal.SIGTERM, reap)
        Path(os.environ["FIXTURE_PID_FILE"]).write_text(str(child.pid))
        time.sleep(30)
    failed = query == "need-login"
    print(json.dumps({"schema_version":2 if query == "schema" else 1,"ok":not failed,
        "channel":args[0],"action":args[1],"backend":"fixture","items":[] if failed else [
            {"title":query,"text":"测试正文","raw":{"private":"fixture-raw"}}],
        "meta":{"argv":args,"supervised":os.environ.get("REACH_API_SUPERVISED"),
                "token_in_child":"REACH_BRIDGE_TOKEN" in os.environ},
        "error":{"code":"NEED_LOGIN","message":"用户需要登录"} if failed else None}))
    sys.exit(1 if failed or query == "exit-mismatch" else 0)
""",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"bilibili": {"hosts": ["www.bilibili.com"]}}))
    monkeypatch.setenv("REACH_BRIDGE_TOKEN", TOKEN)
    return Settings(token=TOKEN, channels_file=channels, executable=str(cli))


def pid_running(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_for_file(path: Path):
    import asyncio
    import time

    def wait():
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("子进程未写入就绪文件")
            time.sleep(0.01)

    await asyncio.to_thread(wait)
