"""Line-delimited JSON-RPC 2.0 client for `codex app-server --listen stdio://`.

The Codex Desktop app bundles the real codex CLI; the app-server protocol
reads and writes the same `~/.codex/sessions/` store the desktop app uses.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from .config import CodexExecutionPolicy, CodexPaths, resolve_execution_policy

STREAM_LIMIT = 64 * 1024 * 1024

NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CodexRpcError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CodexClient:
    def __init__(
        self,
        paths: CodexPaths,
        timeout: float = 30.0,
        execution_policy: CodexExecutionPolicy | None = None,
    ):
        self.paths = paths
        self._timeout = timeout
        self.execution_policy = execution_policy or resolve_execution_policy()
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notification_handlers: set[NotificationHandler] = set()
        self._started = False

    async def start(self) -> None:
        if self._started and self._proc and self._proc.returncode is None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            str(self.paths.bin), *self.execution_policy.app_server_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        asyncio.create_task(self._read_stdout(self._proc))
        asyncio.create_task(self._read_stderr(self._proc))

        await self.request("initialize", {
            "clientInfo": {
                "name": "codex-mcp-server",
                "title": "Codex MCP Server",
                "version": "0.1.0",
            },
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        })
        await self.notify("initialized")
        self._started = True

    async def close(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        self._proc = None
        self._started = False

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        """Replace notification subscribers (kept for older single-turn callers)."""
        self._notification_handlers = {handler}

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        """Subscribe to app-server notifications without disturbing other turns."""
        self._notification_handlers.add(handler)

    def remove_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.discard(handler)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("codex app-server is not started")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"codex rpc {method} timed out after {self._timeout}s")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("codex app-server is not started")
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    # --- domain helpers -------------------------------------------------

    async def list_sessions(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self.request("thread/list", params)

    async def resume(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/resume", {
            "threadId": thread_id,
            **self.execution_policy.thread_settings(),
        })

    async def start_thread(self, cwd: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = self.execution_policy.thread_settings()
        if cwd:
            params["cwd"] = cwd
        return await self.request("thread/start", params)

    async def read_thread(self, thread_id: str, include_turns: bool = True) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})

    async def start_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        return await self.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            **self.execution_policy.turn_settings(),
        })

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        return await self.request("turn/interrupt", {"threadId": thread_id})

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        while line := await proc.stdout.readline():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = payload.get("id")
            if request_id in self._pending and ("result" in payload or "error" in payload):
                future = self._pending.pop(request_id)
                if "error" in payload:
                    err = payload["error"]
                    future.set_exception(CodexRpcError(err.get("code", -32000), err.get("message", str(err))))
                else:
                    future.set_result(payload.get("result") or {})
                continue
            for handler in tuple(self._notification_handlers):
                try:
                    await handler(payload)
                except Exception:
                    pass

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        while line := await proc.stderr.readline():
            if self._debug:
                sys.stderr.write("[codex] " + line.decode(errors="replace").rstrip() + "\n")

    _debug = False
