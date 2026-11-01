"""通过真实 TCP 连接验证服务器启动、请求及断开后的任务回收。"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx

from .conftest import TOKEN, pid_running


def wait_until(predicate, process=None):
    deadline = time.monotonic() + 8
    while not predicate():
        if process is not None and process.poll() is not None:
            raise AssertionError("测试 HTTP 服务意外退出")
        if time.monotonic() >= deadline:
            raise AssertionError("等待本地测试状态超时")
        time.sleep(0.02)


def test_live_http_and_disconnect(settings, tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    pid_file = tmp_path / "tcp-child.pid"
    environment = dict(
        os.environ,
        REACH_BRIDGE_TOKEN=TOKEN,
        REACH_BRIDGE_EXECUTABLE=settings.executable,
        REACH_BRIDGE_CHANNELS_FILE=str(settings.channels_file),
        FIXTURE_PID_FILE=str(pid_file),
    )
    log = (tmp_path / "uvicorn.log").open("wb")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "reach_bridge.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=environment,
        stdout=log,
        stderr=log,
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:

            def ready():
                try:
                    return client.get("/readyz").status_code == 200
                except httpx.ConnectError:
                    return False

            wait_until(ready, server)
            assert client.get("/v1/channels").status_code == 401
            response = client.post(
                "/v1/search",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"platform": "bilibili", "query": "TCP 正文"},
            )
            assert response.status_code == 200
            assert response.json()["items"][0]["title"] == "TCP 正文"
        body = json.dumps({"platform": "bilibili", "query": "spawn"}).encode()
        with socket.create_connection(("127.0.0.1", port)) as connection:
            headers = (
                f"POST /v1/search HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            )
            connection.sendall(headers.encode() + body)
            wait_until(pid_file.exists, server)
            pid = int(pid_file.read_text())
            assert pid_running(pid)
        wait_until(lambda: not pid_running(pid), server)
        assert unrelated.poll() is None
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        unrelated.terminate()
        unrelated.wait(timeout=3)
        log.close()
