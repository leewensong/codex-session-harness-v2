"""Best-effort, read-only Claude JSONL view across all Claude projects."""
from __future__ import annotations
import json
import os
import re
import uuid
from collections import deque
from pathlib import Path
from .contracts import AdaptorError, check_limit

MAX_LINE = 4 * 1024 * 1024


def native_id(value: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
        if parsed != value.lower():
            raise ValueError()
        return parsed
    except (ValueError, AttributeError, TypeError):
        raise AdaptorError("invalid_session_id", "session id must be a canonical UUID") from None


def project_dir_name(folder: str) -> str:
    # Claude stores project histories using the absolute cwd with every
    # non-alphanumeric character replaced by '-'. This matches Windows paths
    # such as D:\\Newproj\\202608\\SmallDesk -> D--Newproj-202608-SmallDesk.
    return re.sub(r"[^a-zA-Z0-9]", "-", str(Path(folder).resolve()))


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))


def _display_project(cwd: str | None, fallback: str) -> str:
    raw = str(cwd or "").rstrip("\\/")
    if raw:
        name = re.split(r"[\\/]", raw)[-1]
        if name:
            return name
    return fallback or "未指定项目"


class History:
    def __init__(self, cwd: Path, projects: Path):
        self.cwd = cwd.resolve()
        self.projects = projects.resolve()
        self.directory = self.projects / project_dir_name(str(self.cwd))

    def path(self, sid: str) -> Path:
        path = self.directory / (native_id(sid) + ".jsonl")
        if not path.resolve().is_relative_to(self.projects):
            raise AdaptorError("invalid_path", "history path escapes configured projects root")
        return path

    def _candidate_paths(self, sid: str):
        """Yield the local owned-project path, then every Desktop project path."""
        sid = native_id(sid)
        local = self.path(sid)
        seen = set()
        if local.is_file():
            seen.add(local.resolve())
            yield local, self.cwd
        try:
            directories = sorted(self.projects.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            directories = []
        for directory in directories:
            if not directory.is_dir():
                continue
            candidate = directory / f"{sid}.jsonl"
            try:
                resolved = candidate.resolve()
                if not resolved.is_relative_to(self.projects) or resolved in seen or not candidate.is_file():
                    continue
            except OSError:
                continue
            seen.add(resolved)
            yield candidate, None

    @staticmethod
    def _read_cwd(path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for _ in range(64):
                    line = f.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                        return obj["cwd"]
        except OSError:
            return None
        return None

    def _read_path(self, path: Path, sid: str, limit: int, expected_cwd: Path | None = None) -> dict:
        check_limit(limit)
        messages = deque(maxlen=limit)
        warnings = 0
        name = sid
        seen = set()
        discovered_cwd = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                while line := f.readline(MAX_LINE + 1):
                    if len(line) > MAX_LINE:
                        while line and not line.endswith("\n"):
                            line = f.readline(MAX_LINE + 1)
                        warnings += 1
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        warnings += 1
                        continue
                    if not isinstance(obj, dict):
                        warnings += 1
                        continue
                    if obj.get("sessionId") and obj["sessionId"] != sid:
                        warnings += 1
                        continue
                    if obj.get("cwd") is not None and not isinstance(obj["cwd"], str):
                        warnings += 1
                        continue
                    row_cwd = obj.get("cwd")
                    if row_cwd:
                        if discovered_cwd is None:
                            discovered_cwd = row_cwd
                        if expected_cwd is not None and os.path.normcase(str(Path(row_cwd).resolve())) != os.path.normcase(str(expected_cwd)):
                            raise AdaptorError("project_mismatch", "history belongs to a different working directory")
                    if obj.get("type") == "custom-title" and isinstance(obj.get("customTitle"), str):
                        name = obj["customTitle"]
                    role = obj.get("type")
                    message = obj.get("message")
                    if role not in {"user", "assistant"} or not isinstance(message, dict) or obj.get("isSidechain") or obj.get("isMeta"):
                        continue
                    text = text_of(message.get("content"))
                    key = obj.get("uuid")
                    if key is not None and not isinstance(key, str):
                        warnings += 1
                        continue
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    if text:
                        messages.append({"role": role, "text": text[:200000], "truncated": len(text) > 200000})
        except OSError as exc:
            raise AdaptorError("session_not_found", "session history could not be read") from exc
        cwd = discovered_cwd or (str(expected_cwd) if expected_cwd is not None else None)
        project_key = path.parent.name
        return {
            "id": sid,
            "name": name,
            "cwd": cwd or "",
            "project": _display_project(cwd, project_key),
            "projectKey": project_key,
            "status": "unknown",
            "messages": list(messages),
            "updatedAt": path.stat().st_mtime,
            "source": "local-jsonl",
            "writable": False,
            "diagnostics": {"parseWarnings": warnings, "branchAware": False},
        }

    def read(self, sid: str, limit: int = 30) -> dict:
        sid = native_id(sid)
        for path, expected_cwd in self._candidate_paths(sid):
            return self._read_path(path, sid, limit, expected_cwd)
        raise AdaptorError("session_not_found", "session not found in configured Claude projects")

    def read_full(self, sid: str) -> dict:
        """Read one point-in-time main JSONL snapshot, retaining every valid record.

        No tail window or text truncation. Tools, attachments, metadata and branch
        references remain in records; messages is the readable projection. Oversize
        histories fail explicitly rather than pretending to be complete.
        """
        sid = native_id(sid)
        for path, expected_cwd in self._candidate_paths(sid):
            summary = self._read_path(path, sid, 1, expected_cwd)
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > 64 * 1024 * 1024:
                    raise AdaptorError("history_too_large", "full history exceeds 64 MiB; use an export/paged reader")
                data = handle.read(size)
            records, messages, seen = [], [], set()
            warnings = 0
            for line_number, line in enumerate(data.splitlines(), 1):
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeError):
                    warnings += 1
                    continue
                if not isinstance(obj, dict):
                    warnings += 1
                    continue
                records.append({"line": line_number, "record": obj})
                if obj.get("sessionId") and obj["sessionId"] != sid:
                    warnings += 1
                    continue
                message = obj.get("message")
                if obj.get("type") not in {"user", "assistant"} or not isinstance(message, dict):
                    continue
                if obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                key = obj.get("uuid")
                if isinstance(key, str) and key:
                    if key in seen:
                        continue
                    seen.add(key)
                content = message.get("content")
                text = text_of(content)
                blocks = content if isinstance(content, list) else []
                has_tools = any(isinstance(b, dict) and b.get("type") in {"tool_use", "tool_result"} for b in blocks)
                if text or blocks:
                    messages.append({"role": "tool" if has_tools and not text else obj["type"],
                                     "text": text, "blocks": blocks, "uuid": key,
                                     "timestamp": obj.get("timestamp"), "truncated": False})
            return {**summary, "messages": messages, "records": records,
                    "diagnostics": {"complete": warnings == 0, "parseWarnings": warnings,
                                    "messageCount": len(messages), "recordCount": len(records),
                                    "snapshotBytes": len(data), "scope": "main-session-jsonl-all-records",
                                    "branchAware": False, "subagentTranscriptsIncluded": False}}
        raise AdaptorError("session_not_found", "session not found in configured Claude projects")

    def list_projects(self) -> list[dict]:
        """Return all Claude project folders, including folders with no sessions."""
        out = []
        try:
            directories = sorted(self.projects.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            directories = []
        for directory in directories:
            if not directory.is_dir():
                continue
            files = list(directory.glob("*.jsonl"))
            cwd = next((self._read_cwd(path) for path in files if path.is_file()), None)
            out.append({
                "id": directory.name,
                "name": _display_project(cwd, directory.name),
                "cwd": cwd or "",
                "sessionCount": len(files),
            })
        return out

    def list(self) -> list[dict]:
        out = []
        try:
            directories = sorted(self.projects.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            directories = []
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.jsonl"):
                try:
                    row = self._read_path(path, path.stem, 1)
                except (AdaptorError, OSError, ValueError):
                    continue
                messages = row.pop("messages")
                row["preview"] = messages[-1]["text"][:200] if messages else ""
                out.append(row)
        return out
