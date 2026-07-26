from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from typing import Iterable

from .chunking import chunk_text, content_hash, frontmatter, markdown_title, stable_id
from .config import Settings
from .models import Document, SourceConfig

logger = logging.getLogger(__name__)


def _safe_resolve(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _skill_id(source: SourceConfig, path: Path) -> str:
    relative = path.relative_to(source.path).as_posix()
    suffix = relative[:-9].strip("/") if relative.endswith("/SKILL.md") else relative
    suffix = suffix or source.name
    return f"{source.name}:{suffix}"


def iter_skills(source: SourceConfig) -> Iterable[Document]:
    for path in sorted(source.path.rglob("SKILL.md")):
        if any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue
        path = _safe_resolve(path, source.path)
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = frontmatter(text)
        skill_id = _skill_id(source, path)
        title = meta.get("name") or markdown_title(text, path.parent.name or source.name)
        description = (meta.get("description") or "").strip()[:1000]
        relative = path.relative_to(source.path).as_posix()
        for index, piece in chunk_text(text):
            yield Document(
                record_id=stable_id(source.kind, source.name, relative, index),
                source_name=source.name,
                kind=source.kind,
                title=title,
                content=piece,
                locator=str(path),
                metadata={
                    "skill_id": skill_id,
                    "description": description,
                    "relative_path": relative,
                    "chunk_index": index,
                    "content_hash": content_hash(piece),
                },
            )


def iter_librarian(source: SourceConfig) -> Iterable[Document]:
    for path in sorted(source.path.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(source.path).parts):
            continue
        path = _safe_resolve(path, source.path)
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(source.path).as_posix()
        title = markdown_title(text, path.stem)
        for index, piece in chunk_text(text):
            yield Document(
                record_id=stable_id(source.kind, source.name, relative, index),
                source_name=source.name,
                kind=source.kind,
                title=title,
                content=piece,
                locator=str(path),
                metadata={
                    "relative_path": relative,
                    "chunk_index": index,
                    "content_hash": content_hash(piece),
                },
            )


def iter_context_mode(source: SourceConfig) -> Iterable[Document]:
    for database in sorted(source.path.glob("*.db")):
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        try:
            query = """
                SELECT c.rowid, c.title, c.content, c.source_id, c.content_type,
                       c.source_category, c.session_id, c.event_id, c.timestamp,
                       s.label, s.file_path, s.content_hash
                  FROM chunks AS c
             LEFT JOIN sources AS s ON s.id = c.source_id
              ORDER BY c.rowid
            """
            for row in connection.execute(query):
                (
                    rowid, title, content, source_id, content_type, source_category,
                    session_id, event_id, timestamp, label, file_path, source_hash,
                ) = row
                text = str(content or "").strip()
                if not text:
                    continue
                canonical = f"{database.name}:{rowid}"
                locator = str(file_path or f"{database}#rowid={rowid}")
                for index, piece in chunk_text(text):
                    yield Document(
                        record_id=stable_id(source.kind, source.name, canonical, index),
                        source_name=source.name,
                        kind=source.kind,
                        title=str(title or label or "context-mode chunk"),
                        content=piece,
                        locator=locator,
                        metadata={
                            "database": database.name,
                            "rowid": int(rowid),
                            "source_id": int(source_id or 0),
                            "content_type": str(content_type or ""),
                            "source_category": str(source_category or ""),
                            "session_id": str(session_id or ""),
                            "event_id": str(event_id or ""),
                            "timestamp": str(timestamp or ""),
                            "source_hash": str(source_hash or ""),
                            "chunk_index": index,
                            "content_hash": content_hash(piece),
                        },
                    )
        finally:
            connection.close()


def iter_hermes_sessions(source: SourceConfig, settings: Settings) -> Iterable[Document]:
    with tempfile.NamedTemporaryFile(prefix="hermes-retrieval-", suffix=".jsonl", delete=False) as handle:
        export_path = Path(handle.name)
    try:
        command = [
            settings.hermes_command,
            "sessions",
            "export",
            str(export_path),
            "--format",
            "jsonl",
            "--redact",
            "--yes",
            "--min-messages",
            "1",
        ]
        if settings.hermes_session_newer_than:
            command.extend(["--newer-than", settings.hermes_session_newer_than])
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Hermes export failed ({result.returncode}): {result.stderr[-1000:]}"
            )
        with export_path.open(encoding="utf-8") as rows:
            for raw in rows:
                if not raw.strip():
                    continue
                session = json.loads(raw)
                session_id = str(session.get("id") or "")
                title = str(session.get("title") or "Hermes session")
                for position, message in enumerate(session.get("messages") or []):
                    role = str(message.get("role") or "")
                    if role not in {"user", "assistant"}:
                        continue
                    text = str(message.get("content") or "").strip()
                    if not text:
                        continue
                    message_id = str(message.get("id") or position)
                    for index, piece in chunk_text(text):
                        canonical = f"{session_id}:{message_id}:{index}"
                        yield Document(
                            record_id=stable_id(source.kind, source.name, canonical),
                            source_name=source.name,
                            kind=source.kind,
                            title=title,
                            content=f"{role}: {piece}",
                            locator=f"hermes-session:{session_id}#message={message_id}",
                            metadata={
                                "session_id": session_id,
                                "message_id": message_id,
                                "role": role,
                                "timestamp": float(message.get("timestamp") or 0),
                                "model": str(session.get("model") or ""),
                                "provider": str(session.get("billing_provider") or ""),
                                "source": str(session.get("source") or ""),
                                "chunk_index": index,
                                "content_hash": content_hash(piece),
                            },
                        )
    finally:
        export_path.unlink(missing_ok=True)


def iter_documents(source: SourceConfig, settings: Settings) -> Iterable[Document]:
    if source.kind == "skills":
        return iter_skills(source)
    if source.kind == "librarian":
        return iter_librarian(source)
    if source.kind == "context_mode":
        return iter_context_mode(source)
    if source.kind == "hermes_sessions":
        return iter_hermes_sessions(source, settings)
    raise ValueError(f"unsupported source kind: {source.kind}")


def skill_catalog(sources: list[SourceConfig]) -> dict[str, tuple[SourceConfig, Path]]:
    catalog: dict[str, tuple[SourceConfig, Path]] = {}
    for source in sources:
        if not source.enabled or source.kind != "skills" or not source.path.is_dir():
            continue
        for path in source.path.rglob("SKILL.md"):
            if any(part in {".git", "node_modules", ".venv"} for part in path.parts):
                continue
            safe_path = _safe_resolve(path, source.path)
            catalog[_skill_id(source, safe_path)] = (source, safe_path)
    return catalog

