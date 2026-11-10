"""codex-mcp-server 的 MCP 客户端封装。

- Streamable HTTP 懒连接（首次调用才连接独立 MCP 服务），长生命周期复用会话
- 5 个 codex 工具透传，返回文本结果
- 每次调用写互动记录（interactions.jsonl）
- codex_list_sessions 成功后刷新任务缓存（tasks.json）
- 失败容忍：工具失败返回错误文本而不是抛异常，不拖垮对话
"""

import asyncio
import json
import logging
import os
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from . import store

RESULT_CAP = 2500  # 注入上下文的工具结果截断长度

LIST_TOOL = "codex_list_sessions"


def _truncate(s: str, n: int) -> str:
    return s[:n] + ("…" if len(s) > n else "")


class CodexMcp:
    def __init__(self, server_dir, endpoint=None):
        self.server_dir = server_dir
        self.endpoint = endpoint or os.environ.get(
            "CODEX_MCP_HTTP_URL",
            "http://127.0.0.1:8765/mcp",
        )
        self._ctx = None  # Streamable HTTP transport 上下文
        self._read = None
        self._write = None
        self._session = None
        self._lock = asyncio.Lock()

    async def connect(self, on_status=None):
        if self._session is not None:
            logging.info("[mcp] reuse existing session")
            return
        if not self.endpoint.startswith(("http://", "https://")):
            raise RuntimeError(f"CODEX_MCP_HTTP_URL 不是有效的 HTTP 地址：{self.endpoint}")
        logging.info("[mcp] connect start url=%s", self.endpoint)
        t0 = time.monotonic()

        async def tick():
            try:
                while True:
                    await asyncio.sleep(2)
                    elapsed = time.monotonic() - t0
                    logging.info("[mcp] still connecting %.0fs", elapsed)
                    if on_status:
                        await on_status(f"[debug] 等待 MCP 连接 {elapsed:.0f}s（uv 拉起 codex-mcp-server）")
            except asyncio.CancelledError:
                return

        ticker = asyncio.create_task(tick())
        try:
            # The MCP service is intentionally outside the Web process. This
            # keeps a Web restart from terminating Codex app-server.
            self._ctx = streamable_http_client(self.endpoint)
            self._read, self._write = await self._ctx.__aenter__()
            session = ClientSession(self._read, self._write)
            self._session = await session.__aenter__()
            await self._session.initialize()
        except Exception:
            await self._drop_connection()
            raise
        finally:
            ticker.cancel()
        logging.info("[mcp] connect ok %.1fs", time.monotonic() - t0)

    async def close(self):
        await self._drop_connection()

    async def _drop_connection(self):
        session, self._session = self._session, None
        ctx, self._ctx = self._ctx, None
        self._read = None
        self._write = None
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 连接已经损坏时只做尽力清理
                pass
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 子进程可能已经退出
                pass

    async def call_tool(
        self,
        name: str,
        args: dict,
        on_status=None,
        result_cap: int | None = RESULT_CAP,
        timeout_seconds: float | None = None,
    ) -> str:
        """调用 MCP 工具，返回文本结果；记录互动，list 后刷新缓存。

        UI callers can set result_cap=None to keep machine-readable JSON intact.
        """
        start = time.monotonic()
        error = ""
        text = ""
        ok = False
        arg_preview = _truncate(json.dumps(args, ensure_ascii=False), 180)
        logging.info("[mcp] call start %s args=%s", name, arg_preview)
        if on_status:
            await on_status(f"[debug] 开始调用 MCP：{name}")

        async def tick():
            try:
                while True:
                    await asyncio.sleep(2)
                    elapsed = time.monotonic() - start
                    logging.info("[mcp] still waiting %s %.0fs", name, elapsed)
                    if on_status:
                        await on_status(f"[debug] 等待 MCP 响应 {name} {elapsed:.0f}s")
            except asyncio.CancelledError:
                return

        ticker = None
        async with self._lock:
            try:
                await self.connect(on_status=on_status)
                ticker = asyncio.create_task(tick())
                call = self._session.call_tool(name, args)
                result = await asyncio.wait_for(call, timeout=timeout_seconds) if timeout_seconds else await call
                text = "".join(c.text for c in result.content if c.type == "text") if hasattr(result, "content") else str(result)
                ok = True
            except asyncio.TimeoutError:
                timeout_text = f"{timeout_seconds:.0f}s" if timeout_seconds else "the configured timeout"
                error = f"{name} timed out after {timeout_text}"
                text = f"工具调用失败：{error}"
                logging.warning("[mcp] call timed out %s", name)
                await self._drop_connection()
            except asyncio.CancelledError:
                await self._drop_connection()
                raise
            except Exception as e:  # noqa: BLE001 - 工具失败不拖垮对话
                logging.warning("[mcp] call failed %s: %s", name, e)
                error = str(e)
                text = f"工具调用失败：{error}"
                await self._drop_connection()
            finally:
                if ticker is not None:
                    ticker.cancel()
        elapsed = round(time.monotonic() - start, 1)
        logging.info("[mcp] call done %s ok=%s %.1fs chars=%d", name, ok, elapsed, len(text))
        if on_status:
            await on_status(f"[debug] MCP {name} 结束 ok={ok} {elapsed}s")
        store.append_interaction({
            "tool": name,
            "args": _truncate(json.dumps(args, ensure_ascii=False), 500),
            "ok": ok,
            "error": _truncate(error, 300),
            "result": _truncate(text, 4000),
            "elapsed": elapsed,
        })
        if ok and name == LIST_TOOL:
            try:
                data = json.loads(text)
                if data.get("ok"):
                    store.save_tasks(data.get("sessions", []))
            except json.JSONDecodeError:
                pass
        return _truncate(text, result_cap) if result_cap is not None else text
