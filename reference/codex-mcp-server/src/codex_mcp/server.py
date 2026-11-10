"""MCP server exposing Codex (ChatGPT desktop) session capabilities.

Tools:
  codex_runtime_status    — codex CLI availability / version / lock overview
  codex_list_sessions     — list Codex sessions with metadata
  codex_start_session     — create a new Codex session (thread)
  codex_read_session      — read a session's recent messages
  codex_send_message      — forward user feedback to a session (runs a turn)
  codex_start_stream_message / codex_poll_stream — start and read reply deltas
  codex_interrupt_turn    — interrupt a running turn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ntpath
import os
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
import uuid

from mcp.server.mcpserver import MCPServer

from .codex_client import CodexClient, CodexRpcError
from .config import CodexExecutionPolicy, CodexPaths, resolve_execution_policy, resolve_paths

mcp = MCPServer(name="codex")

_client: CodexClient | None = None
_paths: CodexPaths | None = None
_execution_policy: CodexExecutionPolicy | None = None
_resumed: set[str] = set()
STREAM_TTL_SECONDS = 15 * 60


@dataclass
class _StreamTurn:
    stream_id: str
    thread_id: str
    created_at: float
    turn_id: str | None = None
    status: str = "starting"
    chunks: list[str] = field(default_factory=list)
    reply: str = ""
    error: str = ""
    finished_at: float | None = None
    task: asyncio.Task | None = None


_stream_turns: dict[str, _StreamTurn] = {}
_RUNTIME_CONTEXT_BLOCK = re.compile(
    r"<(?:environment_context|app-context|skills_instructions|permissions instructions|"
    r"collaboration_mode|plugins_instructions|turn_aborted)>.*?</(?:environment_context|"
    r"app-context|skills_instructions|permissions instructions|collaboration_mode|"
    r"plugins_instructions|turn_aborted)>",
    re.DOTALL,
)


def _get_paths() -> CodexPaths:
    global _paths
    if _paths is None:
        _paths = resolve_paths()
    return _paths


def _get_execution_policy() -> CodexExecutionPolicy:
    global _execution_policy
    if _execution_policy is None:
        _execution_policy = resolve_execution_policy()
    return _execution_policy


def _get_client() -> CodexClient:
    global _client
    if _client is None:
        _client = CodexClient(
            _get_paths(),
            timeout=30.0,
            execution_policy=_get_execution_policy(),
        )
    return _client


def _message_text(content: Any) -> str:
    """Extract concatenated text from a message's content list."""
    if not isinstance(content, list):
        return str(content or "")
    parts = []
    for c in content:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "".join(parts)


def _session_summary(s: dict[str, Any]) -> dict[str, Any]:
    preview = (s.get("preview") or "").strip().splitlines()
    status = s.get("status") or {}
    cwd = s.get("cwd")
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "cwd": cwd,
        "project": s.get("project") or (ntpath.basename(str(cwd).rstrip("\\/")) if cwd else "未指定项目"),
        "status": status.get("type", "unknown"),
        "updatedAt": s.get("updatedAt"),
        "cliVersion": s.get("cliVersion"),
        "source": s.get("source"),
        "preview": preview[0][:200] if preview else "",
    }


def _rollout_paths(paths: CodexPaths) -> dict[str, Path]:
    """Map thread ids to their persisted rollout files."""
    result: dict[str, Path] = {}
    if not paths.sessions_dir.is_dir():
        return result
    for path in paths.sessions_dir.rglob("*.jsonl"):
        thread_id = path.stem[-36:]
        if len(thread_id) != 36 or thread_id.count("-") != 4:
            continue
        current = result.get(thread_id)
        try:
            if current is None or path.stat().st_mtime > current.stat().st_mtime:
                result[thread_id] = path
        except OSError:
            continue
    return result


def _session_index(paths: CodexPaths) -> dict[str, dict[str, Any]]:
    """Read Codex's append-only session title index, keeping the latest row."""
    index_path = paths.home / "session_index.jsonl"
    result: dict[str, dict[str, Any]] = {}
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        thread_id = row.get("id") if isinstance(row, dict) else None
        if isinstance(thread_id, str) and thread_id:
            result[thread_id] = row
    return result


def _rollout_message(record: dict[str, Any]) -> dict[str, str] | None:
    """Extract a user-visible message from a persisted rollout record."""
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload") or {}
    if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
        return None
    text = _RUNTIME_CONTEXT_BLOCK.sub("", _message_text(payload.get("content"))).strip()
    return {"role": payload["role"], "text": text} if text else None


def _rollout_metadata(path: Path) -> tuple[dict[str, Any], str]:
    """Read only enough of a rollout to obtain metadata and its first user preview."""
    metadata: dict[str, Any] = {}
    preview = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream):
                if line_number >= 200:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    metadata = record.get("payload") or {}
                message = _rollout_message(record)
                if message and message["role"] == "user":
                    preview = message["text"].strip().splitlines()[0][:200]
                if metadata and preview:
                    break
    except OSError:
        pass
    return metadata, preview


def _unix_timestamp(value: Any, fallback_path: Path | None = None) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    if fallback_path is not None:
        try:
            return int(fallback_path.stat().st_mtime)
        except OSError:
            pass
    return None


def _local_sessions(paths: CodexPaths) -> list[dict[str, Any]]:
    """Build the session list directly from Codex's read-only local store."""
    index = _session_index(paths)
    rollouts = _rollout_paths(paths)
    # session_index.jsonl is Codex's user-facing session boundary. The rollout
    # tree also contains many internal sub-agent files that must stay hidden.
    thread_ids = list(index) if index else list(rollouts)
    rows: list[tuple[int, dict[str, Any]]] = []
    for thread_id in thread_ids:
        row = index.get(thread_id, {})
        rollout = rollouts.get(thread_id)
        metadata, preview = _rollout_metadata(rollout) if rollout else ({}, "")
        updated_at = _unix_timestamp(row.get("updated_at") or metadata.get("timestamp"), rollout)
        session = {
            "id": thread_id,
            "name": row.get("thread_name") or metadata.get("name") or preview or thread_id,
            "cwd": metadata.get("cwd"),
            "project": ntpath.basename(str(metadata.get("cwd")).rstrip("\\/")) if metadata.get("cwd") else "未指定项目",
            "status": "active" if (paths.lock_dir / f"{thread_id}.lock").exists() else "idle",
            "updatedAt": updated_at,
            "cliVersion": metadata.get("cli_version"),
            "source": metadata.get("source"),
            "preview": preview,
        }
        rows.append((updated_at or 0, session))
    rows.sort(key=lambda item: item[0], reverse=True)
    return [session for _, session in rows]


def _read_local_session(paths: CodexPaths, thread_id: str, max_messages: int) -> dict[str, Any] | None:
    rollout = _rollout_paths(paths).get(thread_id)
    if rollout is None:
        return None

    index_row = _session_index(paths).get(thread_id, {})
    metadata: dict[str, Any] = {}
    messages: deque[dict[str, str]] | list[dict[str, str]]
    messages = deque(maxlen=max_messages) if max_messages > 0 else []
    total_turns = 0
    try:
        with rollout.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    metadata = record.get("payload") or {}
                message = _rollout_message(record)
                if message is None:
                    continue
                if message["role"] == "user":
                    total_turns += 1
                messages.append(message)
    except OSError as exc:
        return {"ok": False, "error": f"Unable to read Codex session data: {exc}"}

    return {
        "ok": True,
        "threadId": thread_id,
        "name": index_row.get("thread_name") or metadata.get("name") or thread_id,
        "cwd": metadata.get("cwd"),
        "project": ntpath.basename(str(metadata.get("cwd")).rstrip("\\/")) if metadata.get("cwd") else "未指定项目",
        "status": "active" if (paths.lock_dir / f"{thread_id}.lock").exists() else "idle",
        "totalTurns": total_turns,
        "messages": list(messages),
        "source": "local-rollout",
        "note": "messages are the last N persisted user/assistant messages; tool events are omitted.",
    }


# --- tools --------------------------------------------------------------


@mcp.tool()
async def codex_runtime_status() -> dict[str, Any]:
    """Report Codex CLI availability, version, and session-store health."""
    paths = _get_paths()
    client = _get_client()
    try:
        await client.start()
        version_out = await asyncio.create_subprocess_exec(
            str(paths.bin), "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _ = await version_out.communicate()
        version = _stdout.decode(errors="replace").strip()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    locks = paths.lock_dir.glob("*.lock") if paths.lock_dir.is_dir() else []
    lock_ids = sorted(p.name.removesuffix(".lock") for p in locks if p.name != ".coordination.lock")
    return {
        "ok": True,
        "bin": str(paths.bin),
        "version": version,
        "codexHome": str(paths.home),
        "sessionsDirExists": paths.sessions_dir.is_dir(),
        "threadsHeldByDesktopApp": len(lock_ids),
        "heldThreadIds": lock_ids,
        "executionPolicy": client.execution_policy.as_dict(),
        "note": ("Threads listed in heldThreadIds have writer-lock files present; if the desktop app is running they are actively open there and cannot be written. "
                 "After the desktop app exits the locks go stale and codex takes over on write. "
                 "The bridge launches app-server and applies the reported policy to new, resumed, and started turns."),
    }


@mcp.tool()
async def codex_list_sessions(
    limit: int = 50,
    cursor: str | None = None,
    name_filter: str | None = None,
) -> dict[str, Any]:
    """List Codex sessions (threads) with metadata. Use name_filter for a case-insensitive substring match."""
    # The app-server list endpoint can be unavailable while the desktop app is
    # upgrading or holding its state database. The rollout index is the same
    # persisted source and lets read-only UI callers remain fast and reliable.
    local_sessions = _local_sessions(_get_paths())
    if local_sessions:
        if name_filter:
            needle = name_filter.lower()
            local_sessions = [
                session for session in local_sessions
                if needle in (session.get("name") or "").lower()
                or needle in (session.get("preview") or "").lower()
            ]
        page_size = max(1, min(int(limit or 50), 200))
        offset = 0
        if isinstance(cursor, str) and cursor.startswith("local:"):
            try:
                offset = max(0, int(cursor.removeprefix("local:")))
            except ValueError:
                offset = 0
        page = local_sessions[offset:offset + page_size]
        next_cursor = f"local:{offset + page_size}" if offset + page_size < len(local_sessions) else None
        return {
            "ok": True,
            "sessions": page,
            "nextCursor": next_cursor,
            "backwardsCursor": None,
            "source": "local-rollout",
        }

    client = _get_client()
    try:
        await client.start()
        res = await client.list_sessions(limit=min(limit, 200), cursor=cursor)
    except Exception as e:  # noqa: BLE001 - surface bridge failures as tool JSON
        return {"ok": False, "error": getattr(e, "message", str(e))}
    sessions = [_session_summary(s) for s in res.get("data", [])]
    if name_filter:
        needle = name_filter.lower()
        sessions = [s for s in sessions if needle in (s["name"] or "").lower() or needle in (s["preview"] or "").lower()]
    return {
        "ok": True,
        "sessions": sessions,
        "nextCursor": res.get("nextCursor"),
        "backwardsCursor": res.get("backwardsCursor"),
    }


@mcp.tool()
async def codex_start_session(
    name: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Create a new Codex session (thread) and return its id.

    cwd sets the working directory for the new session. name is applied via
    thread/name/set (thread/start itself ignores name). The new session is
    empty until codex_send_message runs a first turn on it.
    """
    client = _get_client()
    await client.start()
    try:
        res = await client.start_thread(cwd)
    except CodexRpcError as e:
        return {"ok": False, "error": e.message}
    thread = res.get("thread", {})
    tid = thread.get("id")
    if name and tid:
        try:
            await client.request("thread/name/set", {"threadId": tid, "name": name})
        except CodexRpcError:
            pass
    return {
        "ok": True,
        "threadId": tid,
        "sessionId": thread.get("sessionId"),
        "name": name,
        "cwd": thread.get("cwd"),
        "status": (thread.get("status") or {}).get("type", "unknown"),
    }


@mcp.tool()
async def codex_read_session(thread_id: str, max_messages: int = 30) -> dict[str, Any]:
    """Read a session's recent messages. thread_id is the session's `id` from codex_list_sessions."""
    local_result = _read_local_session(_get_paths(), thread_id, max_messages)
    if local_result is not None:
        return local_result

    client = _get_client()
    try:
        await client.start()
        res = await client.read_thread(thread_id, include_turns=True)
    except Exception as e:  # noqa: BLE001 - surface bridge failures as tool JSON
        return {"ok": False, "error": getattr(e, "message", str(e))}

    thread = res.get("thread", {})
    turns = thread.get("turns", [])
    messages: list[dict[str, str]] = []
    for turn in turns:
        for item in turn.get("items", []):
            item_type = item.get("type")
            if item_type == "userMessage":
                role = "user"
                text = _message_text(item.get("content"))
            elif item_type == "agentMessage":
                role = "assistant"
                text = item.get("text") or _message_text(item.get("content")) or ""
            else:
                continue
            if not text:
                continue
            messages.append({"role": role, "text": text})
    if max_messages > 0:
        messages = messages[-max_messages:]
    return {
        "ok": True,
        "threadId": thread_id,
        "name": thread.get("name"),
        "cwd": thread.get("cwd"),
        "status": (thread.get("status") or {}).get("type", "unknown"),
        "totalTurns": len(turns),
        "messages": messages,
        "note": "messages are the last N (role, text) pairs; tool events are omitted.",
    }


async def _run_turn(thread_id: str, text: str, timeout: float) -> dict[str, Any]:
    client = _get_client()
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    deltas: list[str] = []
    turn_id_holder: dict[str, str | None] = {"id": None}

    async def on_notification(payload: dict[str, Any]) -> None:
        method = payload.get("method")
        params = payload.get("params") or {}
        notification_thread = params.get("threadId")
        if notification_thread and notification_thread != thread_id:
            return
        if method == "turn/started":
            turn = params.get("turn") or {}
            if turn.get("id"):
                turn_id_holder["id"] = turn["id"]
        elif method == "item/agentMessage/delta":
            delta = params.get("delta") or params.get("text") or ""
            if isinstance(delta, str) and delta:
                deltas.append(delta)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            matches_turn = (
                params.get("threadId") == thread_id
                or turn.get("threadId") == thread_id
                or (turn_id_holder["id"] and turn.get("id") == turn_id_holder["id"])
            )
            if matches_turn and not completed.done():
                completed.set_result(turn)

    client.add_notification_handler(on_notification)
    try:
        try:
            result = await client.start_turn(thread_id, text)
        except CodexRpcError as e:
            return {"status": "error", "error": e.message}

        turn_id = (result.get("turn") or {}).get("id") or turn_id_holder["id"]

        # Wait for the turn to complete (or timeout).
        done, _ = await asyncio.wait({completed}, timeout=timeout)
        if not done:
            return {
                "status": "running",
                "threadId": thread_id,
                "turnId": turn_id,
                "error": f"turn still running after {timeout:.0f}s; call codex_interrupt_turn to stop it",
            }

        turn = completed.result()
        items = turn.get("items") or []
        reply = ""
        for item in items:
            if item.get("type") == "agentMessage":
                reply = item.get("text") or _message_text(item.get("content")) or ""
        if not reply and deltas:
            reply = "".join(deltas)
        raw_status = turn.get("status")
        status = raw_status.get("type", "completed") if isinstance(raw_status, dict) else (raw_status or "completed")
        return {
            "status": status,
            "threadId": thread_id,
            "turnId": turn.get("id") or turn_id,
            "reply": reply,
        }
    finally:
        client.remove_notification_handler(on_notification)


def _cleanup_stream_turns() -> None:
    now = time.monotonic()
    for stream_id, state in list(_stream_turns.items()):
        if state.finished_at is not None and now - state.finished_at > STREAM_TTL_SECONDS:
            _stream_turns.pop(stream_id, None)


async def _collect_stream_turn(state: _StreamTurn, text: str) -> None:
    """Collect app-server deltas in memory so the MCP client can poll them."""
    client = _get_client()
    loop = asyncio.get_running_loop()
    completed: asyncio.Future[dict[str, Any]] = loop.create_future()

    async def on_notification(payload: dict[str, Any]) -> None:
        method = payload.get("method")
        params = payload.get("params") or {}
        notification_thread = params.get("threadId")
        if notification_thread and notification_thread != state.thread_id:
            return
        if method == "turn/started":
            turn = params.get("turn") or {}
            if turn.get("id"):
                state.turn_id = turn["id"]
            state.status = "running"
        elif method == "item/agentMessage/delta":
            delta = params.get("delta") or params.get("text") or ""
            if isinstance(delta, str) and delta:
                state.chunks.append(delta)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            matches_turn = (
                params.get("threadId") == state.thread_id
                or turn.get("threadId") == state.thread_id
                or (state.turn_id and turn.get("id") == state.turn_id)
            )
            if matches_turn and not completed.done():
                completed.set_result(turn)

    client.add_notification_handler(on_notification)
    try:
        state.status = "running"
        result = await client.start_turn(state.thread_id, text)
        state.turn_id = (result.get("turn") or {}).get("id") or state.turn_id
        turn = await completed
        items = turn.get("items") or []
        for item in items:
            if item.get("type") == "agentMessage":
                state.reply = item.get("text") or _message_text(item.get("content")) or ""
        if not state.reply and state.chunks:
            state.reply = "".join(state.chunks)
        if state.reply and not state.chunks:
            state.chunks.append(state.reply)
        raw_status = turn.get("status")
        state.status = raw_status.get("type", "completed") if isinstance(raw_status, dict) else (raw_status or "completed")
    except asyncio.CancelledError:
        state.status = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - keep an inspectable stream error for the caller
        state.status = "error"
        state.error = getattr(exc, "message", str(exc))
    finally:
        state.finished_at = time.monotonic()
        client.remove_notification_handler(on_notification)


@mcp.tool()
async def codex_start_stream_message(thread_id: str, message: str) -> dict[str, Any]:
    """Start a Codex turn and expose its agent-message deltas through polling."""
    _cleanup_stream_turns()
    if not thread_id.strip() or not message.strip():
        return {"ok": False, "status": "error", "error": "thread_id and message are required"}
    if any(
        state.thread_id == thread_id and state.status in {"starting", "running"}
        for state in _stream_turns.values()
    ):
        return {"ok": False, "status": "error", "error": "a streamed turn is already running for this session"}

    client = _get_client()
    await client.start()
    if thread_id not in _resumed:
        try:
            await client.resume(thread_id)
            _resumed.add(thread_id)
        except CodexRpcError as exc:
            if "active writer" in exc.message.lower():
                return {
                    "ok": False,
                    "status": "error",
                    "error": (
                        f"session {thread_id} is locked: it is currently open in the Codex Desktop app "
                        "(writer lock held). Close/exit the desktop app (or this session) first, then retry."
                    ),
                }
            return {"ok": False, "status": "error", "error": exc.message}

    stream_id = uuid.uuid4().hex
    state = _StreamTurn(stream_id=stream_id, thread_id=thread_id, created_at=time.monotonic())
    _stream_turns[stream_id] = state
    state.task = asyncio.create_task(_collect_stream_turn(state, message))
    return {
        "ok": True,
        "status": "running",
        "streamId": stream_id,
        "threadId": thread_id,
    }


@mcp.tool()
async def codex_poll_stream(stream_id: str, cursor: int = 0) -> dict[str, Any]:
    """Return newly collected Codex reply deltas after ``cursor``."""
    _cleanup_stream_turns()
    state = _stream_turns.get(stream_id)
    if state is None:
        return {"ok": False, "status": "error", "error": "unknown or expired stream_id"}
    try:
        cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        cursor = 0
    cursor = min(cursor, len(state.chunks))
    chunks = [
        {"index": index, "text": text}
        for index, text in enumerate(state.chunks[cursor:], start=cursor)
    ]
    return {
        "ok": True,
        "status": state.status,
        "streamId": state.stream_id,
        "threadId": state.thread_id,
        "turnId": state.turn_id,
        "chunks": chunks,
        "nextCursor": len(state.chunks),
        "reply": state.reply if state.status not in {"starting", "running"} else "",
        "error": state.error,
    }


@mcp.tool()
async def codex_send_message(
    thread_id: str,
    message: str,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Forward user feedback to a Codex session and wait for the agent's reply.

    Returns status=completed with the agent's final reply, or status=running
    (timeout) so the caller can interrupt, or status=error. If the session is
    open in the Codex Desktop app it is locked and cannot be written.
    """
    client = _get_client()
    await client.start()

    if thread_id not in _resumed:
        try:
            await client.resume(thread_id)
            _resumed.add(thread_id)
        except CodexRpcError as e:
            if "active writer" in e.message.lower():
                return {
                    "ok": False,
                    "status": "error",
                    "error": (
                        f"session {thread_id} is locked: it is currently open in the Codex Desktop app "
                        "(writer lock held). Close/exit the desktop app (or this session) first, then retry."
                    ),
                }
            return {"ok": False, "status": "error", "error": e.message}

    started = time.monotonic()
    result = await _run_turn(thread_id, message, timeout_seconds)
    result["elapsedSeconds"] = round(time.monotonic() - started, 1)
    result["ok"] = result["status"] != "error"
    return result


@mcp.tool()
async def codex_interrupt_turn(thread_id: str) -> dict[str, Any]:
    """Interrupt a turn that is currently running on a session."""
    client = _get_client()
    await client.start()
    try:
        res = await client.interrupt(thread_id)
        return {"ok": True, "result": res}
    except CodexRpcError as e:
        return {"ok": False, "error": e.message}


def _voice_demo_dir() -> Path:
    configured = os.environ.get("VOICE_HARNESS_DEMO_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    # The launcher exports the exact demo directory. Keep a repository-relative
    # fallback for manual development runs.
    return Path(__file__).resolve().parents[3] / "voice-duplex-h5" / "demo"


async def _run_harness_lifecycle(action: str) -> dict[str, Any]:
    """Ask the independent lifecycle manager to operate the H5 service."""
    demo_dir = _voice_demo_dir()
    manager = demo_dir / "manage-services.ps1"
    if not manager.is_file():
        return {"ok": False, "error": f"harness lifecycle manager not found: {manager}"}
    shell = shutil.which("pwsh") or shutil.which("powershell") or "pwsh.exe"
    args = [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(manager),
        "-Target", "harness", "-Action", action, "-Json", "-Wait",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            shell, *args, cwd=str(demo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "harness lifecycle action timed out after 45s"}
    except OSError as exc:
        return {"ok": False, "error": f"cannot start harness lifecycle manager: {exc}"}
    raw = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    result: dict[str, Any] | None = None
    for line in reversed(raw.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            result = candidate
            break
    if result is None:
        result = {"ok": proc.returncode == 0, "output": raw}
    if proc.returncode != 0:
        result["ok"] = False
    if err:
        result["error"] = (str(result.get("error") or "") + " " + err).strip()
    return result


@mcp.tool()
async def harness_service_control(
    action: str,
) -> dict[str, Any]:
    """Start, stop, restart, or inspect the independent H5 harness service.

    The action is executed by a one-shot Windows task, so restarting harness
    does not depend on this MCP process remaining alive after the stop.
    """
    action = (action or "").strip().lower()
    if action not in {"start", "stop", "restart", "status"}:
        return {"ok": False, "error": "action must be start, stop, restart, or status"}
    return await _run_harness_lifecycle(action)


# --- entry point ----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=8765, help="HTTP transport port (default: 8765)")
    args = parser.parse_args()

    try:
        if args.transport == "http":
            asyncio.run(mcp.run_streamable_http_async(host="127.0.0.1", port=args.port))
        else:
            asyncio.run(mcp.run_stdio_async())
    finally:
        global _client
        if _client is not None:
            try:
                asyncio.run(_client.close())
            except Exception:
                pass


if __name__ == "__main__":
    main()
