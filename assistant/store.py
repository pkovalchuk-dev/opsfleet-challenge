"""SQLite store for conversations and reports."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import User

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  content_json TEXT NOT NULL DEFAULT '{"turns": []}', updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  title TEXT NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports(owner_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS reports_turn_step ON reports
  (conversation_id, json_extract(content_json, '$.turn_id'), json_extract(content_json, '$.step_index'));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def report_content(*, turn_id: str, question: str, answer: str, queries: list[dict[str, Any]],
                   brand_scope: list[str] | tuple[str, ...], data_cutoff: str,
                   assumptions: str = "", step_index: int = 0) -> dict[str, Any]:
    """Build a report payload from checked analysis evidence.

    Callers authorize the save and supply provenance from the source analysis.
    """
    return {
        "turn_id": turn_id, "step_index": step_index, "question": question, "answer_md": answer,
        "queries": [{"sql": q.get("sql"), "bq_job_id": q.get("bq_job_id"),
                     "bytes_billed": q.get("bytes_billed")} for q in queries],
        "brand_scope": list(brand_scope), "data_cutoff": data_cutoff,
        "metric_version": "v1", "assumptions": assumptions, "notes": "",
    }


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- conversations ----------------------------------------------------
    def new_conversation(self, owner_id: str, title: str = "") -> str:
        cid = _new_id("c")
        self.conn.execute(
            "INSERT INTO conversations (id, owner_id, title, content_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (cid, owner_id, title, json.dumps({"turns": []}), _now()),
        )
        self.conn.commit()
        return cid

    def get_conversation(self, conversation_id: str, owner_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "owner_id": row["owner_id"], "title": row["title"],
                "content": json.loads(row["content_json"]), "updated_at": row["updated_at"]}

    def append_turn(self, conversation_id: str, owner_id: str, turn: dict[str, Any]) -> None:
        conv = self.get_conversation(conversation_id, owner_id)
        if conv is None:
            raise PermissionError("conversation not found for this user")
        content = conv["content"]
        content.setdefault("turns", []).append(turn)
        title = conv["title"] or turn.get("question", "")[:60]
        self.conn.execute(
            "UPDATE conversations SET content_json = ?, title = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
            (json.dumps(content, default=str), title, _now(), conversation_id, owner_id),
        )
        self.conn.commit()

    def recent_turns(self, conversation_id: str, owner_id: str, limit: int = 10) -> list[dict[str, Any]]:
        conv = self.get_conversation(conversation_id, owner_id)
        if conv is None:
            return []
        turns = conv["content"].get("turns", [])
        return turns[-limit:]

    # -- reports ----------------------------------------------------------
    def save_report(self, owner_id: str, conversation_id: str, title: str, content: dict[str, Any]) -> str:
        """Idempotent on (conversation_id, turn_id, step_index): a retried save returns the existing id."""
        turn_id = content.get("turn_id")
        if turn_id:
            row = self.conn.execute(
                "SELECT id FROM reports WHERE conversation_id = ? AND owner_id = ? AND json_extract(content_json, '$.turn_id') = ? "
                "AND json_extract(content_json, '$.step_index') = ?",
                (conversation_id, owner_id, turn_id, content.get("step_index")),
            ).fetchone()
            if row is not None:
                return row["id"]
        rid = _new_id("r")
        self.conn.execute(
            "INSERT INTO reports (id, owner_id, conversation_id, title, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, owner_id, conversation_id, title, json.dumps(content, default=str), _now()),
        )
        self.conn.commit()
        return rid

    def get_report(self, report_id: str, user: User) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM reports WHERE id = ? AND owner_id = ?", (report_id, user.id)).fetchone()
        return self._report_row(row, user) if row else None

    def list_reports(self, user: User, conversation_id: str | None = None,
                     limit: int | None = None) -> list[dict[str, Any]]:
        return self.find_reports(user, conversation_id=conversation_id, limit=limit)

    def find_reports(self, user: User, *, text: str = "", conversation_id: str | None = None,
                     limit: int | None = None) -> list[dict[str, Any]]:
        """Check scope before matching. Return all matches unless a limit is requested."""
        cap = None if limit is None else max(0, limit)
        if cap == 0:
            return []
        clauses = ["owner_id = ?"]
        params: list[Any] = [user.id]
        reason = "owned by you"
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
            reason = "created in this conversation"
        if text:
            reason = f"mentions '{text}'"
        rows = self.conn.execute(
            "SELECT * FROM reports WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, rowid DESC", params)
        found = []
        for row in rows:
            report = self._report_row(row, user)
            if text:
                if report["restricted"]:
                    continue  # Hidden content must not influence search results.
                content = report["content"]
                fields = (report["title"], content.get("question"), content.get("answer_md"))
                if not any(text.casefold() in str(value or "").casefold() for value in fields):
                    continue
            found.append(dict(report, match_reason=reason))
            if cap is not None and len(found) >= cap:
                break
        return found

    def delete_reports(self, owner_id: str, report_ids: list[str]) -> int:
        if not report_ids:
            return 0
        with self.conn:  # one transaction
            cur = self.conn.execute(
                "DELETE FROM reports WHERE owner_id = ? AND id IN (SELECT value FROM json_each(?))",
                (owner_id, json.dumps(report_ids)))
        return cur.rowcount

    @staticmethod
    def _report_row(row: sqlite3.Row, user: User) -> dict[str, Any]:
        content = json.loads(row["content_json"])
        scope = content.get("brand_scope")
        readable = isinstance(scope, list) and all(isinstance(brand, str) for brand in scope) and user.allows_scope(scope)
        return {"id": row["id"], "owner_id": row["owner_id"], "conversation_id": row["conversation_id"],
                "title": row["title"] if readable else "Restricted report",
                "content": content if readable else {}, "restricted": not readable, "created_at": row["created_at"]}
