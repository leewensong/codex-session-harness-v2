#!/usr/bin/env python3
"""Backward-compatible CLI for the unified Claude Code adaptor.

Examples:
  python bridge.py list <folder>
  python bridge.py read <folder> <session_id>
  python bridge.py new <folder> <text>
  python bridge.py send <folder> <session_id> <text>

All successful and failed operations emit one JSON document.  External Claude
sessions remain read-only; only sessions reserved in the configured state
 directory can be written by ``send``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agent_adaptors.claude import ClaudeCodeAdaptor
from agent_adaptors.contracts import AdaptorError


def _json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _build(args) -> ClaudeCodeAdaptor:
    state_dir = args.state_dir or os.environ.get("CLAUDE_BRIDGE_STATE_DIR") or str(Path(args.folder) / ".claude-bridge")
    projects_dir = args.projects_dir or os.environ.get("CLAUDE_PROJECTS_DIR")
    return ClaudeCodeAdaptor(
        cwd=args.folder,
        state_dir=state_dir,
        projects_dir=projects_dir,
        binary=args.binary,
        env_mode=args.env_mode,
    )


async def _run(args) -> dict:
    adaptor = _build(args)
    try:
        if args.operation == "list":
            return {"ok": True, **await adaptor.list_sessions(limit=args.limit)}
        if args.operation == "read":
            return {"ok": True, **await adaptor.read_session(args.session_id, limit=args.limit)}
        if args.operation == "new":
            session = await adaptor.create_session(name=args.name)
            result = await adaptor.send_turn(session["id"], args.text, timeout=args.timeout)
            return {"ok": result.get("status") == "completed", "session": session, "turn": result}
        if args.operation == "send":
            result = await adaptor.send_turn(args.session_id, args.text, timeout=args.timeout)
            return {"ok": result.get("status") == "completed", "turn": result}
        raise AdaptorError("invalid_argument", "unknown operation")
    finally:
        await adaptor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["list", "read", "new", "send"])
    parser.add_argument("folder")
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--name", default="Claude Code Bridge Session")
    parser.add_argument("--state-dir")
    parser.add_argument("--projects-dir")
    parser.add_argument("--binary")
    parser.add_argument("--env-mode", choices=["inherit", "minimal"], default="inherit")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if args.operation == "read" and not args.session_id:
        parser.error("read requires session_id")
    if args.operation == "send" and (not args.session_id or args.text is None):
        parser.error("send requires session_id and text")
    if args.operation == "new":
        if args.session_id is None or args.text is not None:
            parser.error("new requires a single quoted text argument")
        args.text, args.session_id = args.session_id, None
    if args.operation == "send" and args.session_id == "new":
        args.operation = "new"
    try:
        result = asyncio.run(_run(args))
        _json(result)
        return 0 if result.get("ok") else 2
    except AdaptorError as exc:
        _json(exc.as_dict())
        return 2
    except Exception:
        _json({"ok": False, "error": {"code": "internal_error", "message": "bridge operation failed"}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
