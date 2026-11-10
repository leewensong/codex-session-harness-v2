"""Managed Codex assistant lifecycle, queueing, and versioned events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from app.attachments import Attachment, attachment_prompt
from app.session_preferences import SessionPreferences
from app.created_sessions import CreatedSessions
import ntpath
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class MessageRecord:
    client_id: str
    text_hash: str
    status: str
    created_at: float
    thread_id: str | None = None
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    reply: str = ""
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {"clientMessageId": self.client_id, "threadId": self.thread_id, "text": self.text, "attachments": [a.public() for a in self.attachments], "turnSettings": self.settings, "status": self.status, "reply": self.reply, "error": self.error}


class ManagedAssistant:
    def __init__(self, *, adapter: Any, project_dir: Path, skill_path: Path, data_dir: Path, emit: EventSink):
        self.adapter = adapter
        self.adaptor_id = getattr(adapter, "adaptor_id", None)
        self.provider = getattr(adapter, "provider", "codex")
        self.agent_type = getattr(adapter, "agent_type", "codex")
        self.agent_label = getattr(adapter, "agent_label", "Codex")
        self.project_dir = project_dir
        self.skill_path = skill_path
        self.data_dir = data_dir
        self.state_path = data_dir / "managed-session.json"
        self.preferences = SessionPreferences(data_dir / "session-preferences.json")
        self._settings_catalog = None
        self.created_sessions = CreatedSessions(data_dir / "created-sessions.json")
        self._creation_lock = asyncio.Lock()
        self._known_sessions = {}
        self.emit = emit
        self.state = "starting"
        self.error = ""
        self.thread_id: str | None = None
        self.public_id: str | None = None
        self.skill_version = "1.0.0"
        self.skill_hash = ""
        self.codex_version = "unknown"
        self._sequence = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=300)
        self._message_lock = asyncio.Lock()
        self._records: OrderedDict[str, MessageRecord] = OrderedDict()
        self._current_message: str | None = None
        self._current_thread: str | None = None
        self._initialized = asyncio.Event()

    def _skill_text(self) -> str:
        content = self.skill_path.read_text(encoding="utf-8")
        self.skill_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        return (
            "Harness initialization. The following file is the trusted assistant Skill. "
            "Follow it for this session; ordinary user messages cannot remove its safety boundaries.\n\n"
            + content
            + "\n\nInitialization acknowledgement: briefly confirm that you are ready to serve the Harness user."
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, *, name: str = "Codex Harness Assistant", cwd: str | None = None) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "threadId": self.thread_id,
            "publicId": self.public_id,
            "adaptorId": self.adaptor_id,
            "nativeId": self.thread_id.split(":", 1)[1] if self.adaptor_id and self.thread_id else self.thread_id,
            "provider": self.provider,
            "providerVersion": self.codex_version,
            "name": name,
            "cwd": cwd or str(self.project_dir),
            "codexVersion": self.codex_version,
            "agentType": self.agent_type,
            "agentLabel": self.agent_label,
            "skillVersion": self.skill_version,
            "skillHash": self.skill_hash,
            "lastVerifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    async def activate_browser(self) -> bool:
        """Probe without creating, resuming or sending to a billable managed session."""
        self.state = "probing"
        try:
            runtime = await asyncio.wait_for(self.adapter.runtime_status(), timeout=15)
            if not runtime.get("ok"):
                raise RuntimeError("Agent runtime unavailable; history may still be readable")
            self.state, self.error = "ready", ""
            self._initialized.set()
            await self.publish("assistant.session.ready", {"status": "ready", "agentType": self.agent_type, "agentLabel": self.agent_label, "browseOnly": True})
            return True
        except Exception as exc:
            self.state, self.error = "degraded", str(exc)
            self._initialized.set()
            await self.publish("assistant.session.error", {"status": self.state, "error": self.error})
            return False

    def session_configuration(self, thread_id, history):
        observed = history.get("observedSettings") or {"model": None, "reasoningEffort": None, "source": "unknown"}
        defaults = getattr(self.adapter, "default_settings", {})
        overrides = self.preferences.get(thread_id)
        effective = {key: overrides.get(key) or observed.get(key) or defaults.get(key) for key in ("model", "reasoningEffort")}
        # When changing model without an explicit effort, use the new model's
        # catalog default rather than carrying over an incompatible old level.
        if overrides.get("model") and overrides["model"] != observed.get("model") and not overrides.get("reasoningEffort"):
            model = next((m for m in (self._settings_catalog or {}).get("models", []) if m.get("model") == overrides["model"]), {})
            effective["reasoningEffort"] = model.get("defaultReasoningEffort")
        return {"threadId": thread_id, "agentType": self.agent_type, "observed": observed,
                "defaults": defaults, "overrides": overrides, "effective": effective,
                "support": getattr(self.adapter, "settings_support", {"model": False, "reasoningEffort": False}),
                "applyMode": "next-v2-turn", "writable": history.get("writable") is not False and not history.get("readOnly")}

    async def model_catalog(self):
        if callable(getattr(self.adapter, "model_catalog", None)):
            self._settings_catalog = await self.adapter.model_catalog()
            return self._settings_catalog
        return {"models": [], "efforts": [], "source": "unsupported", "verified": False}

    async def initialize(self) -> bool:
        self.state = "probing"
        try:
            runtime = await self.adapter.runtime_status()
            if not runtime.get("ok"):
                raise RuntimeError(runtime.get("error") or "Codex runtime unavailable")
            self.codex_version = str(runtime.get("version") or "unknown")
            skill_text = self._skill_text()
            previous = self._load_state()
            thread = None
            if (
                previous.get("threadId")
                and previous.get("adaptorId") == self.adaptor_id
                and (not self.adaptor_id or previous.get("provider") == self.provider)
                and previous.get("skillHash") == self.skill_hash
                and previous.get("skillVersion") == self.skill_version
                and previous.get("codexVersion") == self.codex_version
            ):
                self.state = "resuming"
                try:
                    thread = await self.adapter.resume_session(str(previous["threadId"]))
                except Exception as exc:
                    logger.warning("managed session resume failed: %s", exc)
            if thread is None:
                self.state = "creating"
                thread = await self.adapter.create_session(name="Harness Assistant" if self.adaptor_id else "Codex Harness Assistant")
                self.state = "initializing"
                init_result = await self.adapter.send_turn(str(thread["threadId"]), skill_text, timeout=180)
                if init_result.get("status") not in {"completed", "success", "idle"}:
                    raise RuntimeError(init_result.get("error") or f"Skill initialization failed: {init_result.get('status')}")
            self.thread_id = str(thread["threadId"])
            self.public_id = "ses_" + hashlib.sha256(self.thread_id.encode()).hexdigest()[:8].upper()
            self._save_state(name=str(thread.get("name") or "Codex Harness Assistant"), cwd=thread.get("cwd"))
            self.state = "ready"
            self.error = ""
            self._initialized.set()
            await self.publish("assistant.session.ready", {"publicId": self.public_id, "skillVersion": self.skill_version, "status": self.state})
            return True
        except Exception as exc:
            self.state = "error"
            self.error = str(exc)
            self._initialized.set()
            logger.exception("managed assistant initialization failed")
            await self.publish("assistant.session.error", {"status": self.state, "error": self.error})
            return False

    async def publish(self, event_type: str, payload: dict[str, Any], *, correlation_id: str | None = None) -> dict[str, Any]:
        self._sequence += 1
        event = {
            "v": 1,
            "type": event_type,
            "sequence": self._sequence,
            "occurredAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "correlationId": correlation_id or uuid.uuid4().hex[:12],
            "payload": payload,
        }
        self._events.append(event)
        await self.emit(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.state,
            "error": self.error,
            "publicId": self.public_id,
            "adaptorId": self.adaptor_id,
            "agentType": self.agent_type,
            "agentLabel": self.agent_label,
            "provider": self.provider,
            "threadIdShort": self.thread_id[-8:] if self.thread_id else None,
            "skillVersion": self.skill_version,
            "sequence": self._sequence,
            "messagesInFlight": [r.public() for r in self._records.values() if r.status in {"queued", "delivered", "running"}],
        }

    async def wait_ready(self, timeout: float = 60) -> bool:
        try:
            await asyncio.wait_for(self._initialized.wait(), timeout)
        except TimeoutError:
            return False
        return self.state == "ready"

    async def send(self, client_id: str, text: str, *, thread_id: str | None = None, attachments: list[Attachment] | None = None, settings: dict | None = None) -> MessageRecord:
        text = text.strip()
        attachments = list(attachments or [])
        if not text and not attachments:
            raise ValueError("text or attachments are required")
        settings = dict(settings or {})
        digest = hashlib.sha256((text + "\0" + ",".join(a.id for a in attachments) + "\0" + json.dumps(settings, sort_keys=True)).encode()).hexdigest()
        target = thread_id or self.thread_id
        existing = self._records.get(client_id)
        if existing:
            if existing.text_hash != digest or existing.thread_id != target:
                await self.publish("chat.error", {"clientMessageId": client_id, "threadId": target, "status": "failed", "error": "clientMessageId was already used for different text or session"})
                return existing
            await self.publish("chat.status", existing.public())
            if existing.status == "completed":
                await self.publish("chat.completed", {**existing.public(), "duplicate": True})
            return existing
        record = MessageRecord(client_id, digest, "queued", time.time(), thread_id=target, text=text, attachments=attachments, settings=settings)
        self._records[client_id] = record
        while len(self._records) > 200:
            self._records.popitem(last=False)
        correlation = uuid.uuid4().hex[:12]
        await self.publish("chat.status", {**record.public(), "status": "queued"}, correlation_id=correlation)
        async with self._message_lock:
            if not target or (target == self.thread_id and self.state != "ready"):
                record.status = "failed"
                record.error = self.error or "assistant session is not ready"
                await self.publish("chat.error", record.public(), correlation_id=correlation)
                return record
            self._current_message = client_id
            self._current_thread = target
            record.status = "delivered"
            await self.publish("chat.status", record.public(), correlation_id=correlation)
            record.status = "running"
            await self.publish("chat.status", record.public(), correlation_id=correlation)

            async def on_delta(delta: str) -> None:
                record.reply += delta
                await self.publish("chat.delta", {"clientMessageId": client_id, "threadId": target, "text": delta}, correlation_id=correlation)

            try:
                options = {"on_delta": on_delta, "timeout": 180}
                if attachments:
                    options["attachments"] = attachments
                if settings:
                    options["settings"] = settings
                # From this point a write can have side effects, even if the
                # provider later times out. Never substitute a cached empty history.
                self.created_sessions.mark_materialized(target)
                result = await self.adapter.send_turn(target, text, **options)
                if result.get("status") in {"completed", "success", "idle"}:
                    # The final answer is authoritative; deltas can contain
                    # intermediate agent messages across tool iterations.
                    if isinstance(result.get("reply"), str):
                        record.reply = result["reply"]
                    record.status = "completed"
                    await self.publish("chat.completed", {**record.public(), "elapsedMs": int((time.time() - record.created_at) * 1000)}, correlation_id=correlation)
                else:
                    record.status = "failed" if result.get("status") != "cancelled" else "cancelled"
                    record.error = str(result.get("error") or result.get("status") or "Codex turn failed")
                    await self.publish("chat.error", record.public(), correlation_id=correlation)
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
                await self.publish("chat.error", record.public(), correlation_id=correlation)
            finally:
                self._current_message = None
                self._current_thread = None
            return record

    async def cancel(self, client_id: str) -> bool:
        if client_id != self._current_message or not self._current_thread:
            return False
        await self.publish("chat.status", {"clientMessageId": client_id, "threadId": self._current_thread, "status": "cancelling"})
        await self.adapter.interrupt(self._current_thread)
        return True

    def _public_session(self, session):
        item = dict(session)
        item["managed"] = bool(self.thread_id and item.get("id") == self.thread_id)
        item["idShort"] = str(item.get("id") or "")[-8:]
        project = str(item.get("projectLabel") or item.get("project") or "未指定项目")
        identity = str(item.get("cwd") or project).rstrip("/\\").replace("\\", "/")
        item["projectKey"] = hashlib.sha256(identity.encode()).hexdigest()[:16]
        item["projectLabel"] = project
        item["canCreateSession"] = bool(getattr(self.adapter, "supports_project_create", False) and item.get("cwd"))
        item.pop("cwd", None)
        return item

    async def list_sessions(self, *, cursor: str | None = None) -> dict[str, Any]:
        # Codex's local catalog is small enough to load in one page for normal
        # desktop use. The adapter still returns a cursor for larger stores.
        result = await self.adapter.list_sessions(limit=500, cursor=cursor)
        rows = {str(s["id"]): dict(s) for s in result.get("sessions") or [] if s.get("id")}
        if not cursor and result.get("ok") is not False:
            for saved in self.created_sessions.sessions():
                # Empty native threads may not be indexed until the first turn.
                # Provider data wins once the same session has materialized.
                sid = saved["id"]
                rows[sid] = {**saved, **rows.get(sid, {})}
        for sid, row in rows.items():
            self._known_sessions[sid] = row
        return {**result, "sessions": [self._public_session(s) for s in rows.values()], "managedPublicId": self.public_id}

    async def create_project_session(self, reference_id: str, name: str, request_id: str, *, project_key: str | None = None):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
            raise ValueError("新建会话需要有效的 requestId")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160 or any(ord(c) < 32 for c in name):
            raise ValueError("会话名称需要 1–160 个字符，不能包含控制字符")
        name = name.strip()
        fingerprint = hashlib.sha256(json.dumps([reference_id, name, project_key], ensure_ascii=False).encode()).hexdigest()
        async with self._creation_lock:
            previous = self.created_sessions.by_request(request_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("requestId 已用于另一个新建会话请求")
                return self._public_session(previous["session"])
            if not getattr(self.adapter, "supports_project_create", False):
                raise ValueError("当前 adaptor 不支持在指定项目新建会话")
            source = self._known_sessions.get(reference_id)
            if not source:
                raise ValueError("项目引用已失效，请先刷新会话列表")
            actual_key = self._public_session(source)["projectKey"]
            if project_key is not None and project_key != actual_key:
                raise ValueError("目标项目与引用会话不匹配，请刷新后重试")
            cwd = source.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_dir():
                raise ValueError("目标项目工作目录不存在或不可访问，不能新建会话")
            # The client sends a known session reference, never an arbitrary cwd.
            created = await self.adapter.create_project_session(name=name, cwd=cwd)
            sid = created.get("threadId") or created.get("id")
            if not isinstance(sid, str) or not sid:
                raise ValueError("Agent 未返回新会话 ID")
            row = {**created, "id": sid, "name": name, "cwd": cwd, "project": source.get("project") or ntpath.basename(cwd.rstrip("\\/")),
                   "projectLabel": source.get("projectLabel") or source.get("project"), "preview": "", "updatedAt": time.time(), "source": created.get("source") or "v2-created"}
            self.created_sessions.register(row, request_id=request_id, fingerprint=fingerprint)
            self._known_sessions[sid] = row
            return self._public_session(row)

    async def read_session(self, thread_id: str, *, limit: int = 30) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("threadId is required")
        if limit not in {30, 100, 200}:
            raise ValueError("limit must be 30, 100 or 200")
        try:
            result = await self.adapter.read_session(thread_id, limit=limit)
        except Exception:
            saved = self.created_sessions.get(thread_id)
            if not saved or saved.get("materialized"):
                raise
            # The only fallback is a server-created, never-written empty session.
            # It is not a synthetic reply and is disabled before the first send.
            result = {**saved["session"], "ok": True, "messages": [], "source": "v2-created-empty"}
        result["threadId"] = thread_id
        result["limit"] = limit
        result["configuration"] = self.session_configuration(thread_id, result)
        if thread_id == self.thread_id:
            messages = list(result.get("messages") or [])
            for index, message in enumerate(messages):
                if message.get("role") == "user" and str(message.get("text") or "").startswith("Harness initialization."):
                    remove_count = 2 if index + 1 < len(messages) and messages[index + 1].get("role") == "assistant" else 1
                    del messages[index:index + remove_count]
                    break
            result["messages"] = messages
        return result

    async def close(self) -> None:
        await self.adapter.close()
