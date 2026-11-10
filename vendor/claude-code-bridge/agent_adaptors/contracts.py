"""Native IDs inside adaptors; namespaced IDs only at registry/service boundary."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable

Json = dict[str, Any]
DeltaCallback = Callable[[str], Awaitable[None]]
CONTRACT_VERSION = 1


class AdaptorError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

    def as_dict(self) -> Json:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "retryable": self.retryable}}


@dataclass(frozen=True)
class Capabilities:
    create: bool = True
    resume: bool = True
    send: bool = True
    stream: bool = False
    interrupt: str = "owned-process"  # none | owned-process | owned-turn
    list: bool = True
    read: bool = True
    rename: bool = False
    external_session_write: bool = False
    create_mode: str = "reserved"  # reserved | native
    history_source: str = "local-jsonl"

    def as_dict(self) -> Json:
        return asdict(self)


def check_limit(limit: int, maximum: int = 200) -> int:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise AdaptorError("invalid_argument", f"limit must be an integer in 1..{maximum}")
    return limit


def check_timeout(timeout: float) -> float:
    import math
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise AdaptorError("invalid_argument", "timeout must be finite and positive")
    return timeout


@runtime_checkable
class AgentAdaptor(Protocol):
    provider: str
    adaptor_version: str
    capabilities: Capabilities

    async def runtime_status(self) -> Json: ...
    async def create_session(self, *, name: str) -> Json: ...
    async def resume_session(self, session_id: str) -> Json: ...
    async def send_turn(self, session_id: str, text: str, *, on_delta: DeltaCallback | None = None, timeout: float = 180.0) -> Json: ...
    async def interrupt(self, session_id: str) -> Json: ...
    async def list_sessions(self, *, limit: int = 50, cursor: str | None = None) -> Json: ...
    async def read_session(self, session_id: str, *, limit: int = 30) -> Json: ...
    async def close(self) -> None: ...
