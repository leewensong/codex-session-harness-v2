"""Claude CLI JSON/stream-json backend. Existing external sessions are read-only.

Reserved UUIDs and ownership live in bridge state, never in provider history.
Timeout terminates owned process; it does not claim the turn is still running.
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from .contracts import AdaptorError, Capabilities, check_limit, check_timeout
from .history import History, native_id
from .lease import StateLease


def resolve_command(binary: str | None = None) -> list[str]:
    found = binary or shutil.which("claude")
    if not found:
        raise AdaptorError("runtime_unavailable", "Claude executable not found")
    path = Path(found)
    # npm Windows shims can reinterpret command line. Prefer bundled native exe.
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        native = path.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if native.is_file():
            return [str(native)]
        raise AdaptorError("runtime_unavailable", "Set binary to the native Claude executable; shell shims are not supported")
    return [str(path)]


class ClaudeCodeAdaptor:
    provider = "claude-code"
    adaptor_version = "0.1.0"
    capabilities = Capabilities(stream=True)

    def __init__(self, *, cwd: str, state_dir: str, projects_dir: str | None = None,
                 binary: str | None = None, env_mode: str = "inherit", tools: list[str] | None = None):
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise AdaptorError("invalid_config", "cwd must be an existing directory")
        if env_mode not in {"inherit", "minimal"}:
            raise AdaptorError("invalid_config", "env_mode must be inherit or minimal")
        if tools is not None and (not isinstance(tools, list) or not all(isinstance(x, str) for x in tools)):
            raise AdaptorError("invalid_config", "tools must be a list of names")
        self.binary, self.env_mode, self.tools = binary, env_mode, tools
        self.history = History(self.cwd, Path(projects_dir) if projects_dir else Path.home()/".claude/projects")
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lease = StateLease(self.state_dir / "bridge.lock")
        self.state_path = self.state_dir / "sessions.json"
        self.sessions = {}
        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if saved.get("version") != 1 or saved.get("cwd") != str(self.cwd) or not isinstance(saved.get("sessions"), dict):
                    raise ValueError()
                self.sessions = saved["sessions"]
                for sid, row in self.sessions.items():
                    native_id(sid)
                    if not isinstance(row, dict) or not isinstance(row.get("started"), bool) or not isinstance(row.get("name"), str) or not isinstance(row.get("updatedAt"), (int, float)): raise ValueError()
            except (OSError, ValueError, TypeError, AttributeError, AdaptorError):
                self.lease.close()
                raise AdaptorError("invalid_state", "invalid bridge state or cwd mismatch; preserve state for recovery") from None
        self.active: dict[str, asyncio.subprocess.Process | None] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancelled: set[str] = set()
        self.closed = False

    def _env(self):
        if self.env_mode == "inherit":
            return os.environ.copy()
        keys = {"HOME", "PATH", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "PATHEXT", "COMSPEC"}
        return {k:v for k,v in os.environ.items() if k.upper() in keys}

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"version": 1, "cwd": str(self.cwd), "sessions": self.sessions}, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            raise AdaptorError("state_write_failed", "bridge state could not be persisted; do not resend automatically") from None

    def _check(self):
        if self.closed: raise AdaptorError("closed", "adaptor is closed")

    async def _spawn(self, *args):
        return await asyncio.create_subprocess_exec(*resolve_command(self.binary), *args,
            cwd=str(self.cwd), env=self._env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    async def _stop(self, proc):
        if proc.returncode is None:
            try: proc.terminate()
            except ProcessLookupError: pass
            try: await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def runtime_status(self):
        self._check()
        proc = None
        try:
            proc = await self._spawn("--version")
            out, _ = await asyncio.wait_for(proc.communicate(), 8)
            version = out.decode("utf-8", errors="replace").strip()
            return {"ok": proc.returncode == 0, "version": version, "provider": self.provider,
                    "capabilities": self.capabilities.as_dict(), "executionPolicy": {"permissionMode": "dontAsk", "envMode": self.env_mode},
                    "diagnostics": {"historyExists": self.history.directory.is_dir(), "authenticationVerified": False}}
        except (OSError, AdaptorError, asyncio.TimeoutError):
            return {"ok": False, "error": {"code": "runtime_unavailable", "message": "Claude runtime probe failed"}}
        finally:
            if proc is not None and proc.returncode is None: await self._stop(proc)

    def _summary(self, sid):
        row = self.sessions[sid]
        return {"id": sid, "name": row["name"], "cwd": str(self.cwd), "status": "running" if sid in self.active else "idle" if row["started"] else "pending",
                "updatedAt": row["updatedAt"], "writable": True, "materialized": row["started"], "source": "bridge-owned"}

    async def create_session(self, *, name):
        self._check()
        if not isinstance(name, str) or not name.strip():
            raise AdaptorError("invalid_argument", "nonempty session name required")
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"name": str(name)[:200], "started": False, "updatedAt": time.time()}
        self._save()
        return self._summary(sid)

    async def resume_session(self, session_id):
        self._check()
        sid = native_id(session_id)
        if sid not in self.sessions:
            raise AdaptorError("session_not_owned", "external sessions are read-only; create a Harness-owned session")
        if self.sessions[sid]["started"] and not self.history.path(sid).is_file():
            raise AdaptorError("session_not_found", "owned session history is missing")
        return self._summary(sid)

    async def list_sessions(self, *, limit=50, cursor=None):
        self._check()
        check_limit(limit)
        try:
            offset = int(cursor or "0")
            if offset < 0: raise ValueError()
        except (ValueError, TypeError):
            raise AdaptorError("invalid_cursor", "cursor must be a nonnegative offset") from None
        rows = {row["id"]: row for row in await asyncio.to_thread(self.history.list)}
        for sid in self.sessions:
            rows[sid] = {**rows.get(sid, {}), **self._summary(sid)}
        ordered = sorted(rows.values(), key=lambda r:(r["updatedAt"], r["id"]), reverse=True)
        return {"sessions": ordered[offset:offset+limit], "nextCursor": str(offset+limit) if offset+limit < len(ordered) else None,
                "projects": self.history.list_projects(), "consistency": "best-effort"}

    async def read_session(self, session_id, *, limit=30):
        self._check()
        check_limit(limit)
        sid = native_id(session_id)
        if sid in self.sessions and not self.sessions[sid]["started"] and not self.history.path(sid).exists():
            return {**self._summary(sid), "messages": []}
        result = await asyncio.to_thread(self.history.read, sid, limit)
        if sid in self.sessions: result.update(self._summary(sid))
        return result

    async def _receive(self, proc, sid, text, on_delta):
        from .stream import StreamParser
        async def input_data():
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        async def drain_stderr():
            while await proc.stderr.read(65536):
                pass  # Never return raw stderr, which can contain credentials/tool logs.
        async def stdout():
            if on_delta is not None:
                parser = StreamParser(sid)
                try:
                    while raw := await proc.stdout.readline():
                        delta = parser.feed(raw)
                        if delta: await on_delta(delta)
                except (ValueError, asyncio.LimitOverrunError):
                    raise AdaptorError("output_limit", "Claude stream line exceeds reader limit") from None
                return parser.result
            chunks, size = [], 0
            while raw := await proc.stdout.read(65536):
                size += len(raw)
                if size > 4 * 1024 * 1024:
                    raise AdaptorError("output_limit", "Claude result exceeds 4 MiB")
                chunks.append(raw)
            try: return json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise AdaptorError("protocol_error", "invalid Claude JSON") from None
        jobs = [asyncio.create_task(coro) for coro in (input_data(), drain_stderr(), stdout(), proc.wait())]
        try:
            results = await asyncio.gather(*jobs)
            return results[2]
        finally:
            for job in jobs:
                if not job.done(): job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def send_turn(self, session_id, text, *, on_delta=None, timeout=180):
        self._check()
        check_timeout(timeout)
        if not isinstance(text, str) or not text.strip() or len(text) > 200000:
            raise AdaptorError("invalid_argument", "text must contain 1..200000 characters")
        sid = native_id(session_id)
        await self.resume_session(sid)
        if sid in self.active: raise AdaptorError("session_busy", "a turn is already running", retryable=True)
        # Reserve before first await: no race between two senders or interrupt and spawn.
        self.active[sid] = None
        self.tasks[sid] = asyncio.current_task()
        started = time.monotonic()
        proc = None
        streaming = on_delta is not None
        args = ["-p", "--output-format", "stream-json" if streaming else "json", "--permission-mode", "dontAsk"]
        if streaming: args += ["--verbose", "--include-partial-messages"]
        args += ["--resume" if self.sessions[sid]["started"] else "--session-id", sid]
        if self.tools is not None: args += ["--tools", ",".join(self.tools)]
        result = {"status": "error", "sessionId": sid, "reply": ""}
        try:
            proc = await self._spawn(*args)
            self.active[sid] = proc
            if sid in self.cancelled:
                await self._stop(proc)
                return {**result, "status": "cancelled"}
            # Drain both pipes concurrently, with hard per-result/line bounds.
            data = await asyncio.wait_for(self._receive(proc, sid, text, on_delta), max(0.001, timeout - (time.monotonic() - started)))
            if sid in self.cancelled: return {**result, "status": "cancelled"}
            if proc.returncode != 0:
                return {**result, "error": {"code": "provider_error", "message": "Claude process failed"}}
            if not isinstance(data, dict) or data.get("session_id") != sid or data.get("type") != "result":
                return {**result, "error": {"code": "protocol_error", "message": "invalid Claude result or session id"}}
            if data.get("permission_denials"):
                return {**result, "error": {"code": "permission_denied", "message": "tool permission denied; no interactive approval channel"}}
            if data.get("is_error") or data.get("subtype") != "success" or not isinstance(data.get("result"), str):
                return {**result, "error": {"code": "provider_error", "message": "Claude turn did not complete successfully"}}
            self.sessions[sid]["started"] = True
            self.sessions[sid]["updatedAt"] = time.time()
            self._save()
            # Final result is authoritative, not concatenated intermediate messages.
            return {**result, "status": "completed", "reply": data["result"], "elapsedSeconds": time.monotonic()-started}
        except asyncio.TimeoutError:
            if proc is not None: await self._stop(proc)
            return {**result, "status": "cancelled", "error": {"code": "turn_timeout", "message": "owned Claude process terminated on timeout"}}
        except AdaptorError as exc:
            if sid in self.cancelled:
                return {**result, "status": "cancelled", "error": {"code": "interrupted", "message": "owned Claude process interrupted"}}
            raise
        except asyncio.CancelledError:
            if proc is not None: await self._stop(proc)
            raise
        except OSError:
            raise AdaptorError("runtime_unavailable", "could not start Claude process") from None
        finally:
            if proc is not None and proc.returncode is None: await self._stop(proc)
            # Failed/cancelled first turns may still have materialized provider history.
            try:
                if self.history.path(sid).is_file():
                    self.sessions[sid]["started"] = True
                    self.sessions[sid]["updatedAt"] = time.time()
                    self._save()
            finally:
                self.active.pop(sid, None)
                self.tasks.pop(sid, None)
                self.cancelled.discard(sid)

    async def interrupt(self, session_id):
        self._check()
        sid = native_id(session_id)
        if sid not in self.sessions: return {"ok": False, "status": "not-owned"}
        if sid not in self.active: return {"ok": True, "status": "not-running"}
        self.cancelled.add(sid)
        proc = self.active[sid]
        if proc is not None: await self._stop(proc)
        return {"ok": True, "status": "interrupted", "scope": "owned-process", "sideEffectsRolledBack": False}

    async def close(self):
        if self.closed: return
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.lease.close()

