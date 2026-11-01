"""固定 CLI 的有界执行；不经过 Shell，不记录查询或上游原始错误。"""

import asyncio
import contextlib
import json
import os
import signal
from typing import Any

from .config import Settings


class BridgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Runner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.concurrency)
        self.processes: set[asyncio.subprocess.Process] = set()
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        tasks = tuple(self.tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                raise result

    async def _stop(self, proc: asyncio.subprocess.Process) -> None:
        # 上游监督模式让普通子进程留在请求进程组；共享的 detached 浏览器另有生命周期。
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 0.5)
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

        async def discard(stream):
            while await stream.read(65536):
                pass

        # 错误或取消会中断原读取器；终止后排空剩余管道，避免 wait 被管道背压阻塞。
        await asyncio.gather(discard(proc.stdout), discard(proc.stderr), proc.wait())

    async def run(
        self,
        args: list[str],
        *,
        executable: str | None = None,
        input_data: bytes | None = None,
        execution_timeout: float | None = None,
    ) -> dict[str, Any]:
        if self.closed:
            raise BridgeError("STOPPING", "服务正在停止", 503)
        try:
            await asyncio.wait_for(self.slots.acquire(), self.settings.queue_timeout)
        except TimeoutError as exc:
            raise BridgeError("BUSY", "并发已满，请稍后再试", 503) from exc
        proc = None
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            if self.closed:
                raise BridgeError("STOPPING", "服务正在停止", 503)
            env = {k: v for k, v in os.environ.items() if not k.startswith("REACH_BRIDGE_")}
            if os.environ.get("REACH_BRIDGE_BROWSER_EXECUTABLE"):
                env["REACH_BRIDGE_BROWSER_EXECUTABLE"] = os.environ[
                    "REACH_BRIDGE_BROWSER_EXECUTABLE"
                ]
            env["REACH_API_SUPERVISED"] = "1"
            try:
                proc = await asyncio.create_subprocess_exec(
                    executable or self.settings.executable,
                    *args,
                    stdin=asyncio.subprocess.PIPE
                    if input_data is not None
                    else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env=env,
                )
            except OSError as exc:
                raise BridgeError(
                    "NEED_SETUP", "无法启动 Agent-Reach，请管理员检查安装", 503
                ) from exc
            self.processes.add(proc)
            total = 0

            async def read(stream: asyncio.StreamReader, keep: bool) -> bytes:
                nonlocal total
                chunks = []
                while chunk := await stream.read(65536):
                    total += len(chunk)
                    if total > self.settings.max_output_bytes:
                        raise BridgeError("OUTPUT_TOO_LARGE", "上游输出超过大小上限")
                    if keep:
                        chunks.append(chunk)
                return b"".join(chunks)

            async def collect() -> bytes:
                # 同时排空两条管道，避免 stderr 填满造成死锁。
                async with asyncio.TaskGroup() as group:
                    stdout = group.create_task(read(proc.stdout, True))
                    group.create_task(read(proc.stderr, False))
                    group.create_task(proc.wait())
                    if input_data is not None:

                        async def feed():
                            try:
                                proc.stdin.write(input_data)
                                await proc.stdin.drain()
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                            finally:
                                proc.stdin.close()

                        group.create_task(feed())
                return stdout.result()

            try:
                async with asyncio.timeout(execution_timeout or self.settings.timeout):
                    output = await collect()
            except TimeoutError as exc:
                raise BridgeError("TIMEOUT", "Agent-Reach 执行超过总时限", 504) from exc
            except ExceptionGroup as exc:
                errors = exc.subgroup(BridgeError)
                if errors:
                    raise errors.exceptions[0] from exc
                raise
            try:
                result = json.loads(output)
            except (ValueError, UnicodeDecodeError) as exc:
                raise BridgeError("INVALID_UPSTREAM", "Agent-Reach 未返回有效 JSON") from exc
            if (
                not isinstance(result, dict)
                or result.get("schema_version") != 1
                or type(result.get("ok")) is not bool
                or proc.returncode != (0 if result["ok"] else 1)
            ):
                raise BridgeError("INVALID_UPSTREAM", "Agent-Reach 协议版本或退出状态不匹配")
            return result
        finally:
            try:
                if proc is not None:
                    await self._stop(proc)
                    self.processes.discard(proc)
            finally:
                self.tasks.discard(task)
                self.slots.release()
