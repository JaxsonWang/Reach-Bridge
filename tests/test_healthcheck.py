"""健康探针对真实 HTTP 状态和连接失败返回正确退出码，不加载服务端。"""

import http.client
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from reach_bridge import healthcheck


@pytest.fixture
def local_http(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        status = 200
        paths = []

        def do_GET(self):
            self.paths.append(self.path)
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(b"private-response-body")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection

    def connect(host, port, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8080, 3)
        return connection(host, server.server_port, timeout=timeout)

    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", connect)
    try:
        yield Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("status", [200, 302, 403, 503])
def test_http_readiness(local_http, capsys, status):
    local_http.status = status
    assert healthcheck.main() == (0 if status == 200 else 1)
    assert local_http.paths == ["/readyz"]
    output = capsys.readouterr()
    assert "private-response-body" not in output.err + output.out
    assert (f"HTTP {status}" in output.err) == (status != 200)


def test_connection_failure_is_visible(monkeypatch, capsys):
    connection = http.client.HTTPConnection
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        unused_port = reserved.getsockname()[1]

    def connect(host, port, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8080, 3)
        return connection(host, unused_port, timeout=0.1)

    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", connect)
    assert healthcheck.main() == 1
    # 未监听端口在不同宿主上可能被拒绝或静默丢包，两者都必须明确失败。
    error = capsys.readouterr().err
    assert any(name in error for name in ("ConnectionRefusedError", "TimeoutError"))


@pytest.mark.parametrize("error", [TimeoutError("private"), http.client.BadStatusLine("private")])
def test_timeout_and_protocol_error_close_connection(monkeypatch, capsys, error):
    closed = []

    class Connection:
        def request(self, method, path):
            assert (method, path) == ("GET", "/readyz")

        def getresponse(self):
            raise error

        def close(self):
            closed.append(True)

    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", lambda *_a, **_kw: Connection())
    assert healthcheck.main() == 1
    assert closed == [True]
    output = capsys.readouterr().err
    assert type(error).__name__ in output and "private" not in output


def test_probe_does_not_import_fastapi_or_urllib():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from reach_bridge import healthcheck; "
            "assert not {'fastapi', 'urllib.request', 'reach_bridge.app'} & set(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
