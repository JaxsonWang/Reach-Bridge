"""隔离验证已安装上游的 CDP 客户端复用、故障语义与本机 HTTP CPU 成本。"""

import json
import os
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import agent_reach
from agent_reach.runtime import cdp


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.counter_lock:
            self.server.requests += 1
            sequence = self.server.requests
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.2)
        self.send_response(503 if self.path == "/error" else 200)
        body = (
            b"invalid-json"
            if self.path == "/invalid"
            else json.dumps({"path": self.path, "sequence": sequence}).encode()
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 超时测试会先关闭客户端连接，测试 HTTP 服务不把它当作另一项失败。
            if self.path != "/slow":
                raise

    def log_message(self, *_args):
        pass


def benchmark():
    loads = []
    original_load = ssl.SSLContext.load_default_certs

    def load(context, *args, **kwargs):
        loads.append(True)
        return original_load(context, *args, **kwargs)

    count = 30
    values = {}
    with patch.object(ssl.SSLContext, "load_default_certs", load):
        for name in ("baseline", "optimized"):
            cdp._json_opener.cache_clear()
            loads.clear()
            started = time.thread_time()
            previous = 0
            for index in range(count):
                if name == "baseline":
                    # 原实现每次构造；只清测试进程的客户端缓存，不碰业务数据。
                    cdp._json_opener.cache_clear()
                result = cdp._json(f"/benchmark/{index}")
                assert result["path"] == f"/benchmark/{index}"
                assert result["sequence"] > previous, "不能缓存响应或省略实际请求"
                previous = result["sequence"]
            values[name + "_client_cpu_seconds"] = time.thread_time() - started
            values[name + "_ca_loads"] = len(loads)
    assert values["baseline_ca_loads"] == count
    assert values["optimized_ca_loads"] == 1
    ratio = values["optimized_client_cpu_seconds"] / values["baseline_client_cpu_seconds"]
    values.update(requests_per_case=count, client_cpu_reduction_percent=(1 - ratio) * 100)
    print("CDP_CPU=" + json.dumps(values), flush=True)
    assert ratio < 0.8, "同请求工作量下客户端 CPU 成本未明显下降"


def behavior(server):
    first, second = cdp._json("/fresh"), cdp._json("/fresh")
    assert second["sequence"] > first["sequence"]
    assert cdp._json("/redirect")["path"] == "/redirected"
    assert cdp._json("/error") is None
    assert cdp._json("/invalid") is None
    assert cdp._json("/recovered")["path"] == "/recovered"
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = [f"/parallel/{index}" for index in range(12)]
        results = list(executor.map(cdp._json, paths))
    assert [result["path"] for result in results] == paths
    assert len({result["sequence"] for result in results}) == len(paths)

    timeout = cdp._TIMEOUT
    try:
        cdp._TIMEOUT = 0.02
        assert cdp._json("/slow") is None
    finally:
        cdp._TIMEOUT = timeout
    assert cdp._json("/after-timeout")["path"] == "/after-timeout"

    cdp._json_opener.cache_clear()
    count = server.requests
    with patch.object(cdp.urllib.request, "build_opener", side_effect=RuntimeError("fixture")):
        try:
            cdp._json("/constructor-error")
        except RuntimeError as error:
            assert str(error) == "fixture"
        else:
            raise AssertionError("原有客户端构造失败不得被吞掉")
    assert server.requests == count
    cdp._json_opener.cache_clear()
    assert cdp._json("/after-constructor-error")["path"] == "/after-constructor-error"


def main():
    assert agent_reach.__version__ == "1.6.1", "此补丁验收针对固定上游 1.6.1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    server.counter_lock, server.requests = threading.Lock(), 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_url = cdp.CDP_URL
    try:
        cdp.CDP_URL = f"http://127.0.0.1:{server.server_port}"
        try:
            # 即使显式配置代理且无 NO_PROXY，也必须直连本机 CDP。
            with patch.dict(
                os.environ,
                HTTP_PROXY="http://127.0.0.1:1",
                HTTPS_PROXY="http://127.0.0.1:1",
                http_proxy="http://127.0.0.1:1",
                https_proxy="http://127.0.0.1:1",
                NO_PROXY="",
                no_proxy="",
            ):
                benchmark()
                behavior(server)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        assert cdp._json("/closed-server") is None
    finally:
        cdp.CDP_URL = original_url
        cdp._json_opener.cache_clear()
    print("已验证：新鲜响应、并发隔离、禁用代理、重定向、HTTP/JSON/超时/连接/构造失败", flush=True)
    print("运行环境=" + sys.platform, flush=True)


if __name__ == "__main__":
    main()
