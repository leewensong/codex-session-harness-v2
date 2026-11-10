"""Small line-delimited JSON-RPC client for Codex app-server."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from .config import CodexExecutionPolicy, CodexPaths, resolve_execution_policy

NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CodexRpcError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CodexRpcClient:
    def __init__(self, paths: CodexPaths, *, timeout: float = 30.0, policy: CodexExecutionPolicy | None = None):
        self.paths = paths
        self.timeout = timeout
        self.policy = policy or resolve_execution_policy()
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: set[NotificationHandler] = set()
        self._reader_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        self.process = await asyncio.create_subprocess_exec(
            str(self.paths.bin), *self.policy.app_server_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
        )
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self._reader_tasks = [
            asyncio.create_task(self._read_stdout(self.process.stdout)),
            asyncio.create_task(self._read_stderr(self.process.stderr)),
        ]
        await self.request("initialize", {
            "clientInfo": {"name": "codex-session-harness-v2", "title": "Codex Session Harness v2", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        })
        await self.notify("initialized")

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in self._reader_tasks:
            task.cancel()
        self._reader_tasks.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Codex app-server closed"))
        self._pending.clear()

    def add_handler(self, handler: NotificationHandler) -> None:
        self._handlers.add(handler)

    def remove_handler(self, handler: NotificationHandler) -> None:
        self._handlers.discard(handler)

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex app-server is not started")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()
        effective_timeout = self.timeout if timeout is None else max(0.1, timeout)
        try:
            return await asyncio.wait_for(future, effective_timeout)
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"Codex RPC {method} timed out after {effective_timeout:g}s")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex app-server is not started")
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n").encode())
        await self.process.stdin.drain()

    async def list_sessions(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if cursor:
            params["cursor"] = cursor
        return await self.request("thread/list", params, timeout=min(self.timeout, 8.0))

    async def start_thread(self, cwd: str | None = None) -> dict[str, Any]:
        params = self.policy.thread_settings()
        if cwd:
            params["cwd"] = cwd
        return await self.request("thread/start", params)

    async def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/resume", {"threadId": thread_id, **self.policy.thread_settings()})

    async def set_name(self, thread_id: str, name: str) -> dict[str, Any]:
        return await self.request("thread/name/set", {"threadId": thread_id, "name": name})

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=min(self.timeout, 8.0))

    async def start_turn(self, thread_id: str, text: str, *, images: list[str] | None = None, settings: dict | None = None) -> dict[str, Any]:
        return await self.request("turn/start", {
            "threadId": thread_id,
            "input": ([{"type": "text", "text": text}] if text else []) + [{"type": "localImage", "path": path} for path in images or []],
            **self.policy.turn_settings(),
            **({"model": settings["model"]} if settings and settings.get("model") else {}),
            **({"effort": settings["reasoningEffort"]} if settings and settings.get("reasoningEffort") else {}),
        })

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        return await self.request("turn/interrupt", {"threadId": thread_id})

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = payload.get("id")
            if request_id in self._pending and ("result" in payload or "error" in payload):
                future = self._pending.pop(request_id)
                if "error" in payload:
                    error = payload["error"] or {}
                    future.set_exception(CodexRpcError(error.get("code", -32000), error.get("message", str(error))))
                else:
                    future.set_result(payload.get("result") or {})
                continue
            for handler in tuple(self._handlers):
                try:
                    await handler(payload)
                except Exception:
                    pass

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            if __debug__ and False:
                sys.stderr.write("[codex] " + line.decode(errors="replace"))
