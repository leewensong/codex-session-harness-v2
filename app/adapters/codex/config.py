"""Resolve the Codex CLI and execution policy for the local adapter.

This is intentionally local to the new project. It is based on the copied
bridge contract, but the new Harness does not import or modify the old bridge.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
_APPROVAL_POLICIES = {"untrusted", "on-request", "never"}


@dataclass(frozen=True)
class CodexPaths:
    bin: Path
    home: Path
    sessions_dir: Path
    lock_dir: Path
    state_db: Path | None = None
    history_db: Path | None = None


@dataclass(frozen=True)
class CodexExecutionPolicy:
    sandbox: str
    approval_policy: str

    def thread_settings(self) -> dict[str, str]:
        return {"sandbox": self.sandbox, "approvalPolicy": self.approval_policy}

    def turn_settings(self) -> dict[str, object]:
        if self.sandbox == "danger-full-access":
            sandbox_policy: dict[str, object] = {"type": "dangerFullAccess"}
        elif self.sandbox == "workspace-write":
            sandbox_policy = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False}
        else:
            sandbox_policy = {"type": "readOnly", "networkAccess": False}
        return {"sandboxPolicy": sandbox_policy, "approvalPolicy": self.approval_policy}

    def app_server_args(self) -> list[str]:
        return [
            "--sandbox", self.sandbox,
            "--ask-for-approval", self.approval_policy,
            "app-server", "--listen", "stdio://",
        ]

    def as_dict(self) -> dict[str, str]:
        return {"sandbox": self.sandbox, "approvalPolicy": self.approval_policy}


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _read_config(home: Path) -> dict:
    path = home / "config.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _configured_bin(config: dict) -> str | None:
    experimental = config.get("experimental") or {}
    if isinstance(experimental, dict) and experimental.get("codex_cli_path"):
        return str(experimental["codex_cli_path"])
    if config.get("CODEX_CLI_PATH"):
        return str(config["CODEX_CLI_PATH"])
    for server in (config.get("mcp_servers") or {}).values():
        env = server.get("env") if isinstance(server, dict) else None
        if isinstance(env, dict) and env.get("CODEX_CLI_PATH"):
            return str(env["CODEX_CLI_PATH"])
    return None


def resolve_paths() -> CodexPaths:
    home = _codex_home()
    configured = os.environ.get("CODEX_BIN") or _configured_bin(_read_config(home))
    binary = Path(configured).expanduser() if configured else None
    if binary is None or not binary.is_file():
        found = shutil.which("codex")
        binary = Path(found) if found else Path(configured or "codex")
    return CodexPaths(
        binary,
        home,
        home / "sessions",
        home / "thread-writer-locks",
        home / "state_5.sqlite",
        home / "thread_history_1.sqlite",
    )


def _choice(name: str, default: str, choices: set[str]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of {sorted(choices)}; got {value!r}")
    return value


def resolve_execution_policy() -> CodexExecutionPolicy:
    return CodexExecutionPolicy(
        sandbox=_choice("CODEX_MCP_SANDBOX", "workspace-write", _SANDBOX_MODES),
        approval_policy=_choice("CODEX_MCP_APPROVAL_POLICY", "never", _APPROVAL_POLICIES),
    )
