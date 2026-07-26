from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .models import Document, SourceConfig


class ArchiveStore:
    """Durable canonical snapshots for sources that may prune their own data."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    record_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS documents_source
                    ON documents(source_name, active);
                CREATE INDEX IF NOT EXISTS documents_kind
                    ON documents(kind, source_name);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _row_to_document(row: sqlite3.Row | tuple) -> Document:
        record_id, source_name, kind, title, content, locator, metadata_json = row[:7]
        return Document(
            record_id=record_id,
            source_name=source_name,
            kind=kind,
            title=title,
            content=content,
            locator=locator,
            metadata=json.loads(metadata_json),
        )

    def sync(
        self,
        source: SourceConfig,
        documents: Iterable[Document],
        retain_history: bool,
    ) -> dict[str, int]:
        rows = list(documents)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE documents SET active = 0 WHERE source_name = ?",
                (source.name,),
            )
            connection.executemany(
                """
                INSERT INTO documents (
                    record_id, source_name, kind, title, content, locator,
                    metadata_json, first_seen_at, last_seen_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(record_id) DO UPDATE SET
                    source_name = excluded.source_name,
                    kind = excluded.kind,
                    title = excluded.title,
                    content = excluded.content,
                    locator = excluded.locator,
                    metadata_json = excluded.metadata_json,
                    last_seen_at = excluded.last_seen_at,
                    active = 1
                """,
                [
                    (
                        item.record_id,
                        item.source_name,
                        item.kind,
                        item.title,
                        item.content,
                        item.locator,
                        json.dumps(item.metadata, sort_keys=True),
                        now,
                        now,
                    )
                    for item in rows
                ],
            )
            if not retain_history:
                connection.execute(
                    "DELETE FROM documents WHERE source_name = ? AND active = 0",
                    (source.name,),
                )
            connection.commit()
            archived = connection.execute(
                "SELECT count(*) FROM documents WHERE source_name = ?",
                (source.name,),
            ).fetchone()[0]
            inactive = connection.execute(
                "SELECT count(*) FROM documents WHERE source_name = ? AND active = 0",
                (source.name,),
            ).fetchone()[0]
        return {"observed": len(rows), "archived": int(archived), "historical": int(inactive)}

    def documents_for_source(self, source_name: str) -> list[Document]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT record_id, source_name, kind, title, content, locator, metadata_json
                  FROM documents
                 WHERE source_name = ?
                 ORDER BY record_id
                """,
                (source_name,),
            ).fetchall()
        return [self._row_to_document(row) for row in rows]

    def count(self, source_name: str) -> tuple[int, int]:
        with closing(self._connect()) as connection:
            total, active = connection.execute(
                """
                SELECT count(*), coalesce(sum(active), 0)
                  FROM documents
                 WHERE source_name = ?
                """,
                (source_name,),
            ).fetchone()
        return int(total), int(active)

    def neighbors(self, record_id: str, before: int, after: int) -> list[Document]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT record_id, source_name, kind, title, content, locator, metadata_json
                  FROM documents
                 WHERE record_id = ?
                """,
                (record_id,),
            ).fetchone()
            if not row:
                return []
            anchor = self._row_to_document(row)
            rows = connection.execute(
                """
                SELECT record_id, source_name, kind, title, content, locator, metadata_json
                  FROM documents
                 WHERE source_name = ? AND kind = ?
                """,
                (anchor.source_name, anchor.kind),
            ).fetchall()
        candidates = [self._row_to_document(item) for item in rows]
        anchor_meta = anchor.metadata
        if anchor.kind == "hermes_sessions":
            session_id = str(anchor_meta.get("session_id") or "")
            candidates = [
                item
                for item in candidates
                if str(item.metadata.get("session_id") or "") == session_id
            ]
            key = lambda item: (
                int(item.metadata.get("message_position") or 0),
                int(item.metadata.get("chunk_index") or 0),
            )
        elif anchor.kind == "context_mode":
            session_id = str(anchor_meta.get("session_id") or "")
            if session_id:
                candidates = [
                    item
                    for item in candidates
                    if str(item.metadata.get("session_id") or "") == session_id
                ]
            else:
                database = str(anchor_meta.get("database") or "")
                source_id = int(anchor_meta.get("source_id") or 0)
                candidates = [
                    item
                    for item in candidates
                    if str(item.metadata.get("database") or "") == database
                    and int(item.metadata.get("source_id") or 0) == source_id
                ]
            key = lambda item: (
                str(item.metadata.get("timestamp") or ""),
                int(item.metadata.get("rowid") or 0),
                int(item.metadata.get("chunk_index") or 0),
            )
        else:
            return [anchor]
        candidates.sort(key=key)
        try:
            position = next(
                index for index, item in enumerate(candidates) if item.record_id == record_id
            )
        except StopIteration:
            return [anchor]
        return candidates[max(0, position - before):position + after + 1]

