from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Iterable

import chromadb
from chromadb.config import Settings as ChromaSettings

from .config import Settings
from .embeddings import Embedder, build_embedders
from .models import Document, SearchHit, SourceConfig

logger = logging.getLogger(__name__)


def _collection_name(source: SourceConfig, lane: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]+", "-", source.name.lower()).strip("-_")
    return f"hermes-retrieval-{source.kind}-{safe}-{lane}"[:512]


def _primitive_metadata(document: Document) -> dict[str, str | int | float | bool]:
    result: dict[str, str | int | float | bool] = {
        "source_name": document.source_name,
        "kind": document.kind,
        "title": document.title[:1000],
        "locator": document.locator[:4000],
    }
    for key, value in document.metadata.items():
        if isinstance(value, (str, int, float, bool)):
            result[key] = value if not isinstance(value, str) else value[:8000]
    serialized = json.dumps(
        {
            "record_id": document.record_id,
            "content": document.content,
            "metadata": result,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result["index_fingerprint"] = hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()
    return result


class RetrievalIndex:
    def __init__(self, settings: Settings):
        self.settings = settings
        client_settings = ChromaSettings(anonymized_telemetry=False)
        self.client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            ssl=settings.chroma_ssl,
            settings=client_settings,
        )
        self.embedders, self.embedding_errors = build_embedders(settings)

    def heartbeat(self) -> int:
        return int(self.client.heartbeat())

    @property
    def embedding_fingerprints(self) -> dict[str, str]:
        return {
            embedder.lane: embedder.fingerprint
            for embedder in self.embedders
        }

    def _collection(self, source: SourceConfig, embedder: Embedder, create: bool):
        name = _collection_name(source, embedder.lane)
        expected = {
            "hnsw:space": "cosine",
            "source_name": source.name,
            "source_kind": source.kind,
            "embedding_lane": embedder.lane,
            "embedding_url": embedder.url,
            "embedding_model": embedder.model,
            "embedding_dimension": embedder.dimension,
            "embedding_fingerprint": embedder.fingerprint,
        }
        try:
            collection = self.client.get_collection(name)
        except Exception:
            if not create:
                return None
            return self.client.get_or_create_collection(name=name, metadata=expected)
        current = collection.metadata or {}
        if (
            current.get("embedding_fingerprint") == embedder.fingerprint
            and current.get("embedding_dimension") in (None, embedder.dimension)
        ):
            return collection
        if not create:
            return None
        # The collection is a disposable cache scoped to exactly one canonical
        # source. Resetting it cannot remove another source's data.
        self.client.delete_collection(name)
        return self.client.get_or_create_collection(name=name, metadata=expected)

    def sync_documents(
        self,
        source: SourceConfig,
        documents: Iterable[Document],
        batch_size: int = 64,
    ) -> dict[str, Any]:
        documents = list(documents)
        unique = {document.record_id: document for document in documents}
        report: dict[str, Any] = {
            "source": source.name,
            "kind": source.kind,
            "documents": len(unique),
            "lanes": {},
        }
        if not self.embedders:
            raise RuntimeError("no embedding lane is available")
        for embedder in self.embedders:
            collection = self._collection(source, embedder, create=True)
            snapshot = collection.get(include=["metadatas"]) or {}
            existing_ids = list(snapshot.get("ids") or [])
            existing_metadata = list(snapshot.get("metadatas") or [])
            existing = set(existing_ids)
            existing_fingerprints = {
                str(record_id): str((metadata or {}).get("index_fingerprint") or "")
                for record_id, metadata in zip(existing_ids, existing_metadata)
            }
            incoming = set(unique)
            stale = sorted(existing - incoming)
            if stale:
                for start in range(0, len(stale), 500):
                    collection.delete(ids=stale[start:start + 500])
            metadata_by_id = {
                record_id: _primitive_metadata(document)
                for record_id, document in unique.items()
            }
            changed_ids = [
                record_id
                for record_id in sorted(incoming)
                if existing_fingerprints.get(record_id)
                != metadata_by_id[record_id]["index_fingerprint"]
            ]
            ordered = [unique[key] for key in changed_ids]
            for start in range(0, len(ordered), batch_size):
                batch = ordered[start:start + batch_size]
                collection.upsert(
                    ids=[item.record_id for item in batch],
                    documents=[item.content for item in batch],
                    metadatas=[metadata_by_id[item.record_id] for item in batch],
                    embeddings=embedder.encode([item.content for item in batch]),
                )
            report["lanes"][embedder.lane] = {
                "collection": collection.name,
                "count": int(collection.count()),
                "deleted": len(stale),
                "upserted": len(changed_ids),
                "unchanged": len(incoming) - len(changed_ids),
                "fingerprint": embedder.fingerprint,
            }
        return report

    def source_health(
        self,
        source: SourceConfig,
        checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        expected_count = int((checkpoint or {}).get("document_count") or 0)
        expected_fingerprints = dict(
            (checkpoint or {}).get("embedding_fingerprints") or {}
        )
        reasons: list[str] = []
        lanes: dict[str, Any] = {}
        if checkpoint is None:
            reasons.append("checkpoint_missing")
        if not self.embedders:
            reasons.append("embedding_lane_unavailable")
        for embedder in self.embedders:
            collection = self._collection(source, embedder, create=False)
            count = int(collection.count()) if collection is not None else 0
            lane_reasons = []
            if expected_fingerprints.get(embedder.lane) != embedder.fingerprint:
                lane_reasons.append("embedding_fingerprint_changed")
            if collection is None:
                lane_reasons.append("collection_missing")
            elif count != expected_count:
                lane_reasons.append("document_count_mismatch")
            lanes[embedder.lane] = {
                "collection": _collection_name(source, embedder.lane),
                "count": count,
                "expected_count": expected_count,
                "fingerprint": embedder.fingerprint,
                "reasons": lane_reasons,
            }
            reasons.extend(f"{embedder.lane}:{reason}" for reason in lane_reasons)
        return {
            "current": not reasons,
            "reasons": reasons,
            "lanes": lanes,
        }

    def search(
        self,
        query: str,
        sources: Iterable[SourceConfig],
        limit: int,
    ) -> list[SearchHit]:
        candidates: list[SearchHit] = []
        for embedder in self.embedders:
            query_vector = embedder.encode([query])
            for source in sources:
                collection = self._collection(source, embedder, create=False)
                if collection is None:
                    continue
                count = int(collection.count())
                if not count:
                    continue
                result = collection.query(
                    query_embeddings=query_vector,
                    n_results=min(max(limit * 2, limit), count),
                    include=["documents", "metadatas", "distances"],
                )
                ids = (result.get("ids") or [[]])[0]
                docs = (result.get("documents") or [[]])[0]
                metas = (result.get("metadatas") or [[]])[0]
                distances = (result.get("distances") or [[]])[0]
                for record_id, content, metadata, distance in zip(ids, docs, metas, distances):
                    metadata = metadata or {}
                    candidates.append(
                        SearchHit(
                            record_id=record_id,
                            source_name=str(metadata.get("source_name") or source.name),
                            kind=str(metadata.get("kind") or source.kind),
                            title=str(metadata.get("title") or ""),
                            locator=str(metadata.get("locator") or ""),
                            content=str(content or ""),
                            metadata=dict(metadata),
                            distance=float(distance),
                            lane=embedder.lane,
                        )
                    )
        candidates.sort(key=lambda hit: (hit.distance, 0 if hit.lane == "custom" else 1))
        deduped: list[SearchHit] = []
        seen: set[str] = set()
        for hit in candidates:
            if hit.record_id in seen:
                continue
            seen.add(hit.record_id)
            deduped.append(hit)
            if len(deduped) >= limit:
                break
        return deduped

    def status(self, sources: list[SourceConfig]) -> dict[str, Any]:
        try:
            heartbeat = self.heartbeat()
            chroma_error = ""
        except Exception as exc:
            heartbeat = None
            chroma_error = f"{type(exc).__name__}: {exc}"
        rows = []
        for source in sources:
            lane_rows: dict[str, Any] = {}
            for embedder in self.embedders:
                collection = self._collection(source, embedder, create=False)
                lane_rows[embedder.lane] = {
                    "collection": _collection_name(source, embedder.lane),
                    "count": int(collection.count()) if collection is not None else 0,
                    "fingerprint": embedder.fingerprint,
                }
            rows.append(
                {
                    "name": source.name,
                    "kind": source.kind,
                    "path": str(source.path),
                    "enabled": source.enabled,
                    "available": source.path.exists(),
                    "lanes": lane_rows,
                }
            )
        return {
            "chroma": {
                "host": self.settings.chroma_host,
                "port": self.settings.chroma_port,
                "heartbeat": heartbeat,
                "healthy": heartbeat is not None,
                "error": chroma_error,
            },
            "embedders": [
                {
                    "lane": item.lane,
                    "model": item.model,
                    "url": item.url,
                    "dimension": item.dimension,
                    "fingerprint": item.fingerprint,
                }
                for item in self.embedders
            ],
            "embedding_errors": self.embedding_errors,
            "sources": rows,
        }
