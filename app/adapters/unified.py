"""Harness facade and pool for the provider-neutral adaptor registry."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from typing import Any

from app.attachments import attachment_prompt
from app.adapters.local_registry import load_v2_registry as load_registry
from app.session_preferences import observed_claude_settings
from agent_adaptors.contracts import AdaptorError
from agent_adaptors.service import AdaptorService


_AGENT_ALIASES = {
    "codex": "codex",
    "codex-mcp": "codex",
    "codex-native": "codex",
    "claude": "claude",
    "claude-code": "claude",
}
_AGENT_LABELS = {"codex": "Codex", "claude": "Claude Code"}


def public_agent_type(provider: str, configured_type: str | None = None) -> str:
    return _AGENT_ALIASES.get(configured_type or provider, _AGENT_ALIASES.get(provider, provider))


class UnifiedHarnessAdapter:
    """Expose one selected adaptor using the legacy Harness adapter shape."""

    def __init__(self, *, registry, service, adaptor_id: str, owns_service: bool = False):
        self.registry = registry
        self.service = service
        self.adaptor_id = adaptor_id
        self.provider = registry.get(adaptor_id).provider
        self.agent_type = public_agent_type(self.provider)
        self.agent_label = _AGENT_LABELS.get(self.agent_type, self.agent_type)
        self._owns_service = owns_service

    @property
    def _adaptor(self):
        return self.registry.get(self.adaptor_id)

    async def runtime_status(self):
        probe = await self.registry.probe()
        selected = probe["adaptors"].get(self.adaptor_id, {"ok": False})
        return {**selected, "registry": probe, "adaptorId": self.adaptor_id, "agentType": self.agent_type}

    @staticmethod
    def _legacy(row):
        return {**row, "threadId": row["id"]}

    def _managed_route(self, thread_id):
        adaptor, native = self.registry.route(thread_id)
        if thread_id.split(":", 1)[0] != self.adaptor_id:
            raise AdaptorError("adaptor_mismatch", "session belongs to another agent type")
        return adaptor, native

    async def create_session(self, *, name):
        return self._legacy(await self.service.create_session(self.adaptor_id, name=name))

    @property
    def supports_project_create(self):
        return bool(getattr(self._adaptor, "supports_project_create", False))

    async def create_project_session(self, *, name, cwd):
        if not self.supports_project_create:
            raise AdaptorError("unsupported", "当前 adaptor 不支持在指定项目新建会话")
        row = await self._adaptor.create_project_session(name=name, cwd=cwd)
        return self._legacy(self.registry.public(self.adaptor_id, row))

    async def resume_session(self, thread_id):
        self._managed_route(thread_id)
        return self._legacy(await self.service.resume_session(thread_id))

    @property
    def image_mode(self):
        return getattr(self._adaptor, "image_mode", "file-reference")

    @property
    def settings_support(self):
        return getattr(self._adaptor, "settings_support", {"model": False, "reasoningEffort": False})

    @property
    def default_settings(self):
        return getattr(self._adaptor, "default_settings", {"model": None, "reasoningEffort": None})

    async def model_catalog(self):
        if callable(getattr(self._adaptor, "model_catalog", None)):
            return await self._adaptor.model_catalog()
        return {"models": [], "efforts": [], "source": "unsupported", "verified": False}

    async def send_turn(self, thread_id, text, *, on_delta=None, timeout=180, attachments=None, settings=None):
        _, native = self._managed_route(thread_id)
        if callable(getattr(self._adaptor, "send_configured_turn", None)):
            result = await self._adaptor.send_configured_turn(native, text, settings=settings, attachments=attachments, on_delta=on_delta, timeout=timeout)
            return {**result, "threadId": thread_id}
        if settings:
            raise AdaptorError("unsupported", "this adaptor cannot apply model or effort overrides")
        start = await self.service.start_turn(thread_id, attachment_prompt(text, attachments or []), timeout=timeout)
        cursor = 0
        try:
            while True:
                row = await self.service.poll(start["streamId"], cursor=cursor)
                for chunk in row["chunks"]:
                    if on_delta:
                        await on_delta(chunk["text"])
                cursor = row["nextCursor"]
                if row["status"] not in {"starting", "running"}:
                    return {**row, "threadId": thread_id}
                await asyncio.sleep(0.05)
        except BaseException:
            await self.service.interrupt(thread_id)
            raise

    async def interrupt(self, thread_id):
        self._managed_route(thread_id)
        return await self.service.interrupt(thread_id)

    async def list_sessions(self, *, limit=50, cursor=None):
        result = await self.service.list_sessions(self.adaptor_id, limit=limit, cursor=cursor)
        sessions = []
        for row in result.get("sessions", []):
            item = dict(row)
            item["agentType"] = self.agent_type
            item["agentLabel"] = self.agent_label
            # Codex already provides project; Claude history provides cwd.
            if not item.get("project"):
                cwd = str(item.get("cwd") or "")
                item["project"] = Path(cwd.rstrip("\\/")).name if cwd else "未指定目录"
            item["projectLabel"] = item["project"]
            sessions.append(item)
        return {**result, "sessions": sessions, "agentType": self.agent_type, "agentLabel": self.agent_label}

    async def read_session(self, thread_id, *, limit=30):
        self._managed_route(thread_id)
        if callable(getattr(self._adaptor, "read_full_session", None)):
            result = self._legacy(await self.service.read_full_session(thread_id))
            result["observedSettings"] = observed_claude_settings(result)
            result.pop("records", None)
            result["messages"] = [m for m in result.get("messages", []) if m.get("role") in {"user", "assistant", "system"}][-limit:]
            return result
        return self._legacy(await self.service.read_session(thread_id, limit=limit))

    async def close(self):
        if self._owns_service:
            await self.service.close()


class UnifiedAdaptorPool:
    """Load all configured adaptors once and select one for the Harness UI."""

    def __init__(self, *, registry, service, adapters, configured_types, configurations):
        self.registry = registry
        self.service = service
        self.adapters = adapters
        self.configured_types = configured_types
        self.configurations = configurations
        self.runtime_statuses = {}
        self._probe_lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config_path: str):
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        entries = config.get("adaptors", []) if isinstance(config, dict) else []
        registry = load_registry(config_path)
        service = AdaptorService(registry)
        adapters = {}
        configured_types = {}
        configurations = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            aid = entry.get("id")
            if not isinstance(aid, str):
                continue
            configured = public_agent_type(str(entry.get("type") or ""))
            if configured in configured_types:
                continue  # UI v1 selects the first configured instance of each type.
            configured_types[configured] = aid
            configurations[configured] = {k: v for k, v in entry.items()
                if k in {"cwd", "endpoint", "stateDir", "projectsDir", "binary", "envMode", "tools", "backend", "model", "permissionMode", "allowExternalSessions"}}
            cfg = configurations[configured]
            if configured == "claude" and entry.get("v2StateDir"):
                cfg["stateDir"] = entry["v2StateDir"]
            for key in ("cwd", "stateDir", "projectsDir"):
                if isinstance(cfg.get(key), str):
                    cfg[key] = str((Path(config_path).resolve().parent / cfg[key]).resolve())
            if "endpoint" in cfg:
                try:
                    url = urlsplit(cfg["endpoint"])
                    host = url.hostname or ""
                    if ":" in host: host = "[" + host + "]"
                    cfg["endpoint"] = urlunsplit((url.scheme, host + (":" + str(url.port) if url.port else ""), url.path, "", ""))
                except (ValueError, TypeError):
                    cfg["endpoint"] = "[invalid endpoint]"
            if configured == "claude":
                cfg.setdefault("binary", "自动查找 claude")
                cfg.setdefault("envMode", "inherit")
                cfg.setdefault("tools", None)
                cfg.setdefault("permissionMode", "dontAsk")
            configurations[configured]["enabled"] = bool(entry.get("enabled", True))
            if entry.get("enabled", True) and aid in registry.instances and configured not in adapters:
                adapters[configured] = UnifiedHarnessAdapter(registry=registry, service=service, adaptor_id=aid)
        return cls(registry=registry, service=service, adapters=adapters, configured_types=configured_types, configurations=configurations)

    def options(self):
        output = []
        for agent_type, label in _AGENT_LABELS.items():
            aid = self.configured_types.get(agent_type)
            loaded = agent_type in self.adapters
            failure = self.registry.failures.get(aid) if aid else None
            output.append({"type": agent_type, "label": label, "configured": bool(aid), "loaded": loaded, "adaptorId": aid,
                           "error": failure.get("error") if isinstance(failure, dict) else None,
                           "config": self.configurations.get(agent_type, {"enabled": False}),
                           "runtime": self.runtime_statuses.get(aid)})
        return output

    def get_agent_type_for_adaptor(self, adaptor_id: str | None):
        if not adaptor_id:
            return None
        for agent_type, aid in self.configured_types.items():
            if aid == adaptor_id:
                return agent_type
        return None

    def get(self, agent_type: str):
        if agent_type not in self.adapters:
            raise AdaptorError("adaptor_unavailable", f"agent type unavailable: {agent_type}")
        return self.adapters[agent_type]

    async def status_options(self):
        async with self._probe_lock:
            probe = await self.registry.probe()
            self.runtime_statuses = {
                aid: {k: value for k, value in row.items() if k in {"ok", "version", "error"}}
                for aid, row in probe.get("adaptors", {}).items()
            }
        return self.options()

    async def close(self):
        await self.service.close()


def build_unified_pool(config_path: str) -> UnifiedAdaptorPool:
    return UnifiedAdaptorPool.from_config(config_path)


def build_unified_adapter(config_path, adaptor_id=None):
    pool = build_unified_pool(config_path)
    if adaptor_id:
        selected = next((a for a in pool.adapters.values() if a.adaptor_id == adaptor_id), None)
    else:
        selected = next(iter(pool.adapters.values()), None)
    if selected is None:
        raise AdaptorError("invalid_config", "no enabled adaptor is available")
    return selected
