"""Resolve the Codex CLI binary and local paths.

The Codex Desktop app bundles the real `codex.exe` CLI and records it in
`~/.codex/config.toml` as `CODEX_CLI_PATH`. Prefer that binary so the MCP
server shares the same session store and CLI version as the desktop app.
"""

from __future__ import annotations

import os
import re
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


@dataclass(frozen=True)
class CodexExecutionPolicy:
    """Codex sandbox and approval settings used by the bridge."""

    sandbox: str
    approval_policy: str

    def thread_settings(self) -> dict[str, str]:
        """Settings accepted by ``thread/start`` and ``thread/resume``."""
        return {
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
        }

    def turn_settings(self) -> dict[str, object]:
        """Settings accepted by ``turn/start`` for the selected sandbox."""
        sandbox_policy: dict[str, object]
        if self.sandbox == "danger-full-access":
            sandbox_policy = {"type": "dangerFullAccess"}
        elif self.sandbox == "workspace-write":
            sandbox_policy = {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
            }
        else:
            sandbox_policy = {"type": "readOnly", "networkAccess": False}
        return {
            "sandboxPolicy": sandbox_policy,
            "approvalPolicy": self.approval_policy,
        }

    def app_server_args(self) -> list[str]:
        """Global Codex CLI flags must precede the ``app-server`` command."""
        return [
            "--sandbox", self.sandbox,
            "--ask-for-approval", self.approval_policy,
            "app-server", "--listen", "stdio://",
        ]

    def as_dict(self) -> dict[str, str]:
        return {"sandbox": self.sandbox, "approvalPolicy": self.approval_policy}


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def _read_config_toml(home: Path) -> dict:
    cfg_path = home / "config.toml"
    if not cfg_path.is_file():
        return {}
    try:
        return tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_cli_path(cfg: dict) -> str | None:
    """Locate the bundled codex.exe recorded anywhere in config.toml.

    The desktop app writes the path under [experimental].codex_cli_path,
    as a top-level CODEX_CLI_PATH, or inside [mcp_servers.<name>.env].
    """
    for candidate in (
        (cfg.get("experimental", {}) or {}).get("codex_cli_path"),
        cfg.get("CODEX_CLI_PATH"),
        *(env.get("CODEX_CLI_PATH") for s in (cfg.get("mcp_servers") or {}).values() if (env := s.get("env"))),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _resolve_bin() -> Path:
    env = os.environ.get("CODEX_BIN")
    if env and Path(env).is_file():
        return Path(env)

    cfg = _read_config_toml(_codex_home())
    cli_path = _find_cli_path(cfg)
    if cli_path and Path(cli_path).is_file():
        return Path(cli_path)

    # Fall back to config-style key the desktop app writes under [experimental].
    found = shutil.which("codex")
    if found:
        return Path(found)
    return Path("codex")


def resolve_paths() -> CodexPaths:
    home = _codex_home()
    bin_path = _resolve_bin()
    return CodexPaths(
        bin=bin_path,
        home=home,
        sessions_dir=home / "sessions",
        lock_dir=home / "thread-writer-locks",
    )


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        expected = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of: {expected}; got {value!r}")
    return value


def resolve_execution_policy() -> CodexExecutionPolicy:
    """Resolve bridge policy, defaulting to the user-approved full-access mode.

    The environment variables remain available for an explicit lower-privilege
    deployment without having to change source code.
    """
    return CodexExecutionPolicy(
        sandbox=_env_choice("CODEX_MCP_SANDBOX", "danger-full-access", _SANDBOX_MODES),
        approval_policy=_env_choice("CODEX_MCP_APPROVAL_POLICY", "never", _APPROVAL_POLICIES),
    )
