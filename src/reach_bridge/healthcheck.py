"""只检查本地 HTTP 就绪，不为每次健康探测加载 TLS 信任库或服务依赖。"""

import http.client
import sys


def main():
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=3)
    try:
        connection.request("GET", "/readyz")
        status = connection.getresponse().status
        if status != 200:
            print(f"GET /readyz: HTTP {status}", file=sys.stderr)
            return 1
        return 0
    except (OSError, http.client.HTTPException) as error:
        print(f"GET /readyz: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
