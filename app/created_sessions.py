"""Keep newly created empty sessions visible before provider history is materialized."""
from __future__ import annotations

import json
from pathlib import Path


class CreatedSessions:
    def __init__(self, path: Path):
        self.path = path
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.rows = saved["sessions"] if saved.get("version") == 1 else {}
            if not isinstance(self.rows, dict):
                self.rows = {}
        except (OSError, ValueError, KeyError, TypeError):
            self.rows = {}

    def _save(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "sessions": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.rows = rows

    def register(self, session, *, request_id: str, fingerprint: str):
        self._save({**self.rows, session["id"]: {
            "session": dict(session), "requestId": request_id, "fingerprint": fingerprint,
            "materialized": False,
        }})

    def by_request(self, request_id):
        return next((row for row in self.rows.values() if row.get("requestId") == request_id), None)

    def get(self, session_id):
        return self.rows.get(session_id)

    def sessions(self):
        return [dict(row["session"]) for row in self.rows.values() if isinstance(row.get("session"), dict)]

    def mark_materialized(self, session_id):
        row = self.rows.get(session_id)
        if row and not row.get("materialized"):
            self._save({**self.rows, session_id: {**row, "materialized": True}})
