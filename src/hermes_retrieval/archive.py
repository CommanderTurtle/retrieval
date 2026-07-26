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
                CREATE INDEX IF NOT EXISTS documents_hermes_timeline
                    ON documents (
                        source_name,
                        json_extract(metadata_json, '$.session_id'),
                        coalesce(
                            json_extract(metadata_json, '$.profile'),
                            'default'
                        ),
                        CAST(
                            coalesce(
                                json_extract(metadata_json, '$.sequence'),
                                0
                            ) AS INTEGER
                        ),
                        CAST(
                            coalesce(
                                json_extract(metadata_json, '$.chunk_index'),
                                0
                            ) AS INTEGER
                        )
                    )
                    WHERE kind = 'hermes_sessions';
                CREATE INDEX IF NOT EXISTS documents_context_timeline
                    ON documents (
                        source_name,
                        json_extract(metadata_json, '$.database'),
                        json_extract(metadata_json, '$.session_id'),
                        CAST(
                            coalesce(
                                json_extract(metadata_json, '$.source_id'),
                                0
                            ) AS INTEGER
                        ),
                        CAST(
                            coalesce(
                                json_extract(metadata_json, '$.rowid'),
                                0
                            ) AS INTEGER
                        ),
                        CAST(
                            coalesce(
                                json_extract(metadata_json, '$.chunk_index'),
                                0
                            ) AS INTEGER
                        )
                    )
                    WHERE kind = 'context_mode';
                CREATE TABLE IF NOT EXISTS source_checkpoints (
                    source_name TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    observed_fingerprint TEXT NOT NULL,
                    synced_fingerprint TEXT NOT NULL,
                    embedding_fingerprints_json TEXT NOT NULL,
                    document_count INTEGER NOT NULL DEFAULT 0,
                    last_observed_at TEXT NOT NULL,
                    last_synced_at TEXT,
                    last_error TEXT NOT NULL DEFAULT ''
                );
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

    @staticmethod
    def _checkpoint_row(row: sqlite3.Row | tuple | None) -> dict | None:
        if not row:
            return None
        (
            source_name,
            source_kind,
            source_path,
            observed_fingerprint,
            synced_fingerprint,
            embedding_fingerprints_json,
            document_count,
            last_observed_at,
            last_synced_at,
            last_error,
        ) = row
        try:
            embedding_fingerprints = json.loads(embedding_fingerprints_json)
        except (TypeError, ValueError):
            embedding_fingerprints = {}
        if not isinstance(embedding_fingerprints, dict):
            embedding_fingerprints = {}
        return {
            "source_name": str(source_name),
            "source_kind": str(source_kind),
            "source_path": str(source_path),
            "observed_fingerprint": str(observed_fingerprint),
            "synced_fingerprint": str(synced_fingerprint),
            "embedding_fingerprints": {
                str(key): str(value)
                for key, value in embedding_fingerprints.items()
            },
            "document_count": int(document_count),
            "last_observed_at": str(last_observed_at),
            "last_synced_at": str(last_synced_at or ""),
            "last_error": str(last_error or ""),
        }

    def checkpoint(self, source_name: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT source_name, source_kind, source_path,
                       observed_fingerprint, synced_fingerprint,
                       embedding_fingerprints_json, document_count,
                       last_observed_at, last_synced_at, last_error
                  FROM source_checkpoints
                 WHERE source_name = ?
                """,
                (source_name,),
            ).fetchone()
        return self._checkpoint_row(row)

    def checkpoints(self) -> dict[str, dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT source_name, source_kind, source_path,
                       observed_fingerprint, synced_fingerprint,
                       embedding_fingerprints_json, document_count,
                       last_observed_at, last_synced_at, last_error
                  FROM source_checkpoints
                 ORDER BY source_name
                """
            ).fetchall()
        return {
            checkpoint["source_name"]: checkpoint
            for row in rows
            if (checkpoint := self._checkpoint_row(row)) is not None
        }

    def mark_sync_success(
        self,
        source: SourceConfig,
        *,
        fingerprint: str,
        embedding_fingerprints: dict[str, str],
        document_count: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO source_checkpoints (
                    source_name, source_kind, source_path,
                    observed_fingerprint, synced_fingerprint,
                    embedding_fingerprints_json, document_count,
                    last_observed_at, last_synced_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(source_name) DO UPDATE SET
                    source_kind = excluded.source_kind,
                    source_path = excluded.source_path,
                    observed_fingerprint = excluded.observed_fingerprint,
                    synced_fingerprint = excluded.synced_fingerprint,
                    embedding_fingerprints_json =
                        excluded.embedding_fingerprints_json,
                    document_count = excluded.document_count,
                    last_observed_at = excluded.last_observed_at,
                    last_synced_at = excluded.last_synced_at,
                    last_error = ''
                """,
                (
                    source.name,
                    source.kind,
                    str(source.path),
                    fingerprint,
                    fingerprint,
                    json.dumps(embedding_fingerprints, sort_keys=True),
                    int(document_count),
                    now,
                    now,
                ),
            )
            connection.commit()

    def mark_sync_error(
        self,
        source: SourceConfig,
        *,
        fingerprint: str,
        error: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO source_checkpoints (
                    source_name, source_kind, source_path,
                    observed_fingerprint, synced_fingerprint,
                    embedding_fingerprints_json, document_count,
                    last_observed_at, last_synced_at, last_error
                ) VALUES (?, ?, ?, ?, '', '{}', 0, ?, NULL, ?)
                ON CONFLICT(source_name) DO UPDATE SET
                    source_kind = excluded.source_kind,
                    source_path = excluded.source_path,
                    observed_fingerprint = excluded.observed_fingerprint,
                    last_observed_at = excluded.last_observed_at,
                    last_error = excluded.last_error
                """,
                (
                    source.name,
                    source.kind,
                    str(source.path),
                    fingerprint,
                    now,
                    error[:4000],
                ),
            )
            connection.commit()

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
            anchor_meta = anchor.metadata
            if anchor.kind == "hermes_sessions":
                rows = connection.execute(
                    """
                    SELECT record_id, source_name, kind, title, content,
                           locator, metadata_json
                      FROM documents
                     WHERE source_name = ?
                       AND kind = 'hermes_sessions'
                       AND json_extract(metadata_json, '$.session_id') = ?
                       AND coalesce(
                               json_extract(metadata_json, '$.profile'),
                               'default'
                           ) = ?
                  ORDER BY CAST(
                               coalesce(
                                   json_extract(metadata_json, '$.sequence'),
                                   0
                               ) AS INTEGER
                           ),
                           CAST(
                               coalesce(
                                   json_extract(
                                       metadata_json,
                                       '$.message_position'
                                   ),
                                   0
                               ) AS INTEGER
                           ),
                           CAST(
                               coalesce(
                                   json_extract(
                                       metadata_json,
                                       '$.chunk_index'
                                   ),
                                   0
                               ) AS INTEGER
                           )
                    """,
                    (
                        anchor.source_name,
                        str(anchor_meta.get("session_id") or ""),
                        str(anchor_meta.get("profile") or "default"),
                    ),
                ).fetchall()
            elif anchor.kind == "context_mode":
                session_id = str(anchor_meta.get("session_id") or "")
                database = str(anchor_meta.get("database") or "")
                if session_id:
                    rows = connection.execute(
                        """
                        SELECT record_id, source_name, kind, title, content,
                               locator, metadata_json
                          FROM documents
                         WHERE source_name = ?
                           AND kind = 'context_mode'
                           AND json_extract(
                                   metadata_json,
                                   '$.database'
                               ) = ?
                           AND json_extract(
                                   metadata_json,
                                   '$.session_id'
                               ) = ?
                      ORDER BY CAST(
                                   coalesce(
                                       json_extract(
                                           metadata_json,
                                           '$.rowid'
                                       ),
                                       0
                                   ) AS INTEGER
                               ),
                               CAST(
                                   coalesce(
                                       json_extract(
                                           metadata_json,
                                           '$.chunk_index'
                                       ),
                                       0
                                   ) AS INTEGER
                               )
                        """,
                        (anchor.source_name, database, session_id),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT record_id, source_name, kind, title, content,
                               locator, metadata_json
                          FROM documents
                         WHERE source_name = ?
                           AND kind = 'context_mode'
                           AND json_extract(
                                   metadata_json,
                                   '$.database'
                               ) = ?
                           AND CAST(
                                   coalesce(
                                       json_extract(
                                           metadata_json,
                                           '$.source_id'
                                       ),
                                       0
                                   ) AS INTEGER
                               ) = ?
                      ORDER BY CAST(
                                   coalesce(
                                       json_extract(
                                           metadata_json,
                                           '$.rowid'
                                       ),
                                       0
                                   ) AS INTEGER
                               ),
                               CAST(
                                   coalesce(
                                       json_extract(
                                           metadata_json,
                                           '$.chunk_index'
                                       ),
                                       0
                                   ) AS INTEGER
                               )
                        """,
                        (
                            anchor.source_name,
                            database,
                            int(anchor_meta.get("source_id") or 0),
                        ),
                    ).fetchall()
            else:
                return [anchor]
        candidates = [self._row_to_document(item) for item in rows]
        try:
            position = next(
                index for index, item in enumerate(candidates) if item.record_id == record_id
            )
        except StopIteration:
            return [anchor]
        return candidates[max(0, position - before):position + after + 1]
