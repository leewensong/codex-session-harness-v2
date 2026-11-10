"""Allowlisted factories, instance isolation and strict public session routing."""
from __future__ import annotations
import asyncio
import re
from collections.abc import Callable
from .contracts import AgentAdaptor, AdaptorError, CONTRACT_VERSION, check_timeout

Factory = Callable[[dict], AgentAdaptor]


class AdaptorRegistry:
    def __init__(self):
        self.instances: dict[str, AgentAdaptor] = {}
        self.failures: dict[str, dict] = {}
        self._closed = False

    @classmethod
    def from_config(cls, config: dict, factories: dict[str, Factory]) -> "AdaptorRegistry":
        if not isinstance(config, dict) or type(config.get("contractVersion")) is not int or config.get("contractVersion") != CONTRACT_VERSION or not isinstance(config.get("adaptors"), list):
            raise AdaptorError("invalid_config", "contractVersion=1 and adaptors array required")
        seen = set()
        for entry in config["adaptors"]:
            if not isinstance(entry, dict):
                raise AdaptorError("invalid_config", "adaptor config must be an object")
            aid = entry.get("id", "")
            if not isinstance(aid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", aid) or aid in seen:
                raise AdaptorError("invalid_config", "invalid or duplicate adaptor id")
            if type(entry.get("enabled", True)) is not bool:
                raise AdaptorError("invalid_config", "enabled must be boolean")
            seen.add(aid)
        registry = cls()
        for entry in config["adaptors"]:
            if not entry.get("enabled", True):
                continue
            aid = entry["id"]
            try:
                factory = factories.get(entry.get("type"))
                if factory is None:
                    raise AdaptorError("unsupported_adaptor", "adaptor type is not registered")
                registry.register(aid, factory(entry))
            except Exception as exc:
                registry.failures[aid] = {"ok": False, "error": {"code": getattr(exc, "code", "load_failed"), "message": "adaptor configuration or dependency unavailable"}}
        return registry

    def register(self, aid: str, adaptor: AgentAdaptor) -> None:
        if self._closed or not re.fullmatch(r"[a-zA-Z0-9_-]+", aid) or aid in self.instances or aid in self.failures:
            raise AdaptorError("invalid_config", "closed registry or duplicate/invalid adaptor id")
        if not isinstance(adaptor, AgentAdaptor):
            raise AdaptorError("contract_mismatch", "factory does not implement AgentAdaptor v1")
        self.instances[aid] = adaptor

    def get(self, aid: str) -> AgentAdaptor:
        if self._closed:
            raise AdaptorError("closed", "registry is closed")
        if aid not in self.instances:
            raise AdaptorError("adaptor_unavailable", "unknown or unavailable adaptor")
        return self.instances[aid]

    def route(self, public_id: str) -> tuple[AgentAdaptor, str]:
        if not isinstance(public_id, str) or ":" not in public_id:
            raise AdaptorError("invalid_session_id", "namespaced adaptorId:nativeId required")
        aid, native = public_id.split(":", 1)
        if not native:
            raise AdaptorError("invalid_session_id", "empty native session id")
        return self.get(aid), native

    def public(self, aid: str, row: dict) -> dict:
        native = row.get("id")
        if not isinstance(native, str) or not native:
            raise AdaptorError("protocol_error", "adaptor returned no native session id")
        return {**row, "id": f"{aid}:{native}", "nativeId": native, "adaptorId": aid, "provider": self.get(aid).provider}

    async def probe(self, *, timeout: float = 10) -> dict:
        check_timeout(timeout)
        async def one(aid, adaptor):
            try:
                value = await asyncio.wait_for(adaptor.runtime_status(), timeout)
                if not isinstance(value, dict): raise AdaptorError("protocol_error", "invalid runtime status")
            except Exception as exc:
                value = {"ok": False, "error": {"code": getattr(exc, "code", "probe_failed"), "message": "runtime probe failed or timed out"}}
            return aid, {**value, "provider": adaptor.provider, "adaptorVersion": adaptor.adaptor_version, "capabilities": adaptor.capabilities.as_dict()}
        statuses = dict(await asyncio.gather(*(one(a, b) for a, b in self.instances.items())))
        statuses.update(self.failures)
        available = sum(bool(v.get("ok")) for v in statuses.values())
        return {"contractVersion": CONTRACT_VERSION, "status": "ready" if available and available == len(statuses) else "degraded" if available else "error", "adaptors": statuses}

    async def close(self) -> dict:
        if self._closed:
            return {}
        self._closed = True
        async def one(aid, adaptor):
            try:
                await adaptor.close()
                return aid, {"ok": True}
            except Exception:
                return aid, {"ok": False, "error": "close_failed"}
        return dict(await asyncio.gather(*(one(a, b) for a, b in self.instances.items())))
