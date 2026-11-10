"""Read-only fallback for Codex's persisted session store.

Codex Desktop and the app-server share this store.  The app-server's
thread/list and thread/read can become unavailable while another Codex writer
is active, but listing and reading persisted history should remain available.
"""

from __future__ import annotations

import json
import ntpath
import re
import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CodexPaths
from app.attachments import message_images

_RUNTIME_CONTEXT_BLOCK = re.compile(
    r"(?:^|\n)\s*<(?:environment_context|app-context|skills_instructions|"
    r"permissions instructions|collaboration_mode|plugins_instructions|turn_aborted)>"
    r".*?</(?:environment_context|app-context|skills_instructions|permissions instructions|"
    r"collaboration_mode|plugins_instructions|turn_aborted)>\s*", re.DOTALL,
)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    return "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _rollout_message(record: dict[str, Any]) -> dict[str, str] | None:
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload") or {}
    if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
        return None
    text = _RUNTIME_CONTEXT_BLOCK.sub("", _message_text(payload.get("content"))).strip()
    images = message_images(payload.get("content"))
    if not text and not images:
        return None
    message = {"role": str(payload["role"]), "text": text}
    if images:
        message["images"] = images
    return message


def _rollout_paths(paths: CodexPaths) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not paths.sessions_dir.is_dir():
        return result
    try:
        candidates = paths.sessions_dir.rglob("*.jsonl")
    except OSError:
        return result
    for path in candidates:
        thread_id = path.stem[-36:]
        if len(thread_id) != 36 or thread_id.count("-") != 4:
            continue
        try:
            current = result.get(thread_id)
            if current is None or path.stat().st_mtime > current.stat().st_mtime:
                result[thread_id] = path
        except OSError:
            continue
    return result


def _session_index(paths: CodexPaths) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        lines = (paths.home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
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


def _sqlite_path(paths: CodexPaths, attribute: str, filename: str) -> Path:
    configured = getattr(paths, attribute, None)
    return Path(configured) if configured else paths.home / filename


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        # Path.as_uri() keeps Windows drive letters and backslashes valid for
        # SQLite's URI parser. The database is never opened for writing.
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        return connection
    except (OSError, sqlite3.Error):
        return None


def _state_threads(paths: CodexPaths) -> dict[str, dict[str, Any]]:
    """Read Codex's complete thread catalog, including older and archived rows.

    session_index.jsonl is a compatibility index and does not contain the
    complete project/cwd catalog. Newer Codex versions persist that catalog in
    state_*.sqlite, which is also the source used by Desktop for archived
    threads, pinning and project metadata.
    """
    path = _sqlite_path(paths, "state_db", "state_5.sqlite")
    connection = _open_readonly(path)
    if connection is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        rows = connection.execute(
            """
            SELECT id, name, title, updated_at, updated_at_ms, archived,
                   rollout_path, cwd, preview, model, reasoning_effort,
                   source, cli_version
            FROM threads
            ORDER BY COALESCE(updated_at_ms, updated_at * 1000, 0) DESC
            """
        )
        for row in rows:
            thread_id = row["id"]
            if isinstance(thread_id, str) and thread_id:
                result[thread_id] = dict(row)
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return result


def _state_row(paths: CodexPaths, thread_id: str) -> dict[str, Any]:
    return _state_threads(paths).get(thread_id, {})


def _timestamp(value: Any, fallback: Path | None = None) -> int | None:
    if isinstance(value, (int, float)):
        numeric = int(value)
        # Codex's SQLite catalog stores *_at_ms while JSONL metadata and the
        # web client convention use seconds. Keep the adapter output stable.
        return numeric // 1000 if abs(numeric) >= 1_000_000_000_000 else numeric
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    if fallback is not None:
        try:
            return int(fallback.stat().st_mtime)
        except OSError:
            pass
    return None


def _rollout_metadata(path: Path | None) -> tuple[dict[str, Any], str]:
    if path is None:
        return {}, ""
    metadata: dict[str, Any] = {}
    preview = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= 200:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    metadata = record.get("payload") or {}
                message = _rollout_message(record)
                if message and message["role"] == "user" and message["text"] and not preview:
                    preview = message["text"].splitlines()[0][:200]
                if metadata and preview:
                    break
    except OSError:
        pass
    return metadata, preview


def _state_session(paths: CodexPaths, thread_id: str, row: dict[str, Any], index_row: dict[str, Any], rollout: Path | None) -> dict[str, Any]:
    metadata, rollout_preview = _rollout_metadata(rollout)
    cwd = row.get("cwd") or metadata.get("cwd")
    preview = row.get("preview") or index_row.get("preview") or rollout_preview
    updated_value = row.get("updated_at_ms") or row.get("updated_at") or index_row.get("updated_at") or metadata.get("timestamp")
    updated_at = _timestamp(updated_value, rollout)
    archived = bool(row.get("archived"))
    name = (
        row.get("name")
        or index_row.get("thread_name")
        or row.get("title")
        or metadata.get("name")
        or preview
        or thread_id
    )
    return {
        "id": thread_id,
        "name": str(name),
        "cwd": cwd,
        "project": ntpath.basename(str(cwd).rstrip("\\/")) if cwd else "未指定项目",
        "status": "active" if (paths.lock_dir / f"{thread_id}.lock").exists() else "idle",
        "updatedAt": updated_at,
        "cliVersion": row.get("cli_version") or metadata.get("cli_version"),
        "source": row.get("source") or metadata.get("source"),
        "preview": str(preview or "")[:200],
        "archived": archived,
        "model": row.get("model"),
        "reasoningEffort": row.get("reasoning_effort"),
    }


def list_local_sessions(paths: CodexPaths, *, limit: int = 100, cursor: str | None = None) -> dict[str, Any] | None:
    index = _session_index(paths)
    rollouts = _rollout_paths(paths)
    state = _state_threads(paths)
    if not index and not rollouts and not state:
        return None
    thread_ids = set(index) | set(rollouts) | set(state)
    rows: list[tuple[int, dict[str, Any]]] = []
    for thread_id in thread_ids:
        row = index.get(thread_id, {})
        rollout = rollouts.get(thread_id)
        session = _state_session(paths, thread_id, state.get(thread_id, {}), row, rollout)
        rows.append((_timestamp(session.get("updatedAt")) or 0, session))
    rows.sort(key=lambda item: item[0], reverse=True)
    sessions = [session for _, session in rows]
    offset = 0
    if isinstance(cursor, str) and cursor.startswith("local:"):
        try:
            offset = max(0, int(cursor.removeprefix("local:")))
        except ValueError:
            offset = 0
    page_size = max(1, min(int(limit or 100), 1000))
    page = sessions[offset:offset + page_size]
    next_cursor = f"local:{offset + page_size}" if offset + page_size < len(sessions) else None
    source = "local-sqlite" if state else "local-rollout"
    return {"ok": True, "sessions": page, "nextCursor": next_cursor, "source": source}


def _history_message(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    if item_type == "userMessage":
        role = "user"
        content = item.get("content")
    elif item_type == "agentMessage":
        role = "assistant"
        content = item.get("content")
        if not content and isinstance(item.get("text"), str):
            content = [{"type": "text", "text": item["text"]}]
    else:
        return None
    text = _RUNTIME_CONTEXT_BLOCK.sub("", _message_text(content)).strip()
    images = message_images(content)
    if not text and not images:
        return None
    message: dict[str, Any] = {"role": role, "text": text}
    if images:
        message["images"] = images
    return message


def _read_history_sqlite(paths: CodexPaths, thread_id: str, *, limit: int, state_row: dict[str, Any]) -> dict[str, Any] | None:
    path = _sqlite_path(paths, "history_db", "thread_history_1.sqlite")
    connection = _open_readonly(path)
    if connection is None:
        return None
    messages: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
    total_turns = 0
    saw_rows = False
    try:
        rows = connection.execute(
            "SELECT item_json FROM thread_items WHERE thread_id = ? ORDER BY rollout_ordinal",
            (thread_id,),
        )
        for row in rows:
            saw_rows = True
            try:
                item = json.loads(row["item_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            message = _history_message(item)
            if message is None:
                continue
            if message["role"] == "user":
                total_turns += 1
            messages.append(message)
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not saw_rows:
        return None
    cwd = state_row.get("cwd")
    return {
        "ok": True,
        "threadId": thread_id,
        "name": state_row.get("name") or state_row.get("title") or thread_id,
        "cwd": cwd,
        "project": ntpath.basename(str(cwd).rstrip("\\/")) if cwd else "未指定项目",
        "status": "idle",
        "totalTurns": total_turns,
        "observedSettings": {
            "model": state_row.get("model"),
            "reasoningEffort": state_row.get("reasoning_effort"),
            "source": "codex-state.sqlite",
        },
        "messages": list(messages),
        "source": "local-sqlite",
        "note": "读取的是 Codex 本地历史索引中的最近 N 条用户/助手消息，工具执行日志已省略。",
    }


def read_local_session(paths: CodexPaths, thread_id: str, *, limit: int = 30) -> dict[str, Any] | None:
    state_row = _state_row(paths, thread_id)
    sqlite_result = _read_history_sqlite(paths, thread_id, limit=limit, state_row=state_row)
    if sqlite_result is not None:
        return sqlite_result
    rollout = _rollout_paths(paths).get(thread_id)
    if rollout is None:
        return None
    index_row = _session_index(paths).get(thread_id, {})
    metadata: dict[str, Any] = {}
    messages: deque[dict[str, str]] = deque(maxlen=max(1, limit))
    total_turns = 0
    observed = {"model": None, "reasoningEffort": None, "source": "unknown"}
    try:
        with rollout.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    metadata = record.get("payload") or {}
                if record.get("type") == "turn_context":
                    context = record.get("payload") or {}
                    collaboration = (context.get("collaboration_mode") or {}).get("settings") or {}
                    model = context.get("model") or collaboration.get("model")
                    effort = context.get("effort") or context.get("reasoning_effort") or collaboration.get("reasoning_effort")
                    observed["model"] = model if isinstance(model, str) else None
                    observed["reasoningEffort"] = effort if isinstance(effort, str) else None
                    observed["source"] = "codex-turn-context"
                message = _rollout_message(record)
                if message is None:
                    continue
                if message["role"] == "user":
                    total_turns += 1
                messages.append(message)
    except OSError as exc:
        return {"ok": False, "error": f"无法读取 Codex 会话文件：{exc}"}
    cwd = metadata.get("cwd")
    return {
        "ok": True,
        "threadId": thread_id,
        "name": index_row.get("thread_name") or metadata.get("name") or thread_id,
        "cwd": cwd,
        "project": ntpath.basename(str(cwd).rstrip("\\/")) if cwd else "未指定项目",
        "status": "active" if (paths.lock_dir / f"{thread_id}.lock").exists() else "idle",
        "totalTurns": total_turns,
        "observedSettings": observed,
        "messages": list(messages),
        "source": "local-rollout",
        "note": "读取的是最近 N 条持久化用户/助手消息，工具执行日志已省略。",
    }
