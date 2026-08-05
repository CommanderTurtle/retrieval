from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

from .archive import ArchiveStore
from .catalog import IweCatalog
from .config import Settings
from .index import RetrievalIndex
from .models import SourceConfig
from .projection import SkillProjection
from .refresh import (
    SourceRefreshWatcher,
    external_watcher_snapshot,
    source_fingerprint,
)
from .sources import (
    iter_documents,
    skill_bundle_files,
    skill_catalog,
    workflow_catalog,
)
from .scout import RetrievalScout


class RetrievalService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.sources = self.settings.sources()
        self.archive = ArchiveStore(self.settings.archive_db)
        self.index = RetrievalIndex(self.settings)
        self.catalog = IweCatalog(self.settings, self.sources)
        self.projections = SkillProjection(self.settings)
        self.scout = RetrievalScout(self.settings)
        self._write_mutex = threading.RLock()
        self._watcher: SourceRefreshWatcher | None = None

    def start_watcher(self) -> None:
        if self._watcher is None:
            self._watcher = SourceRefreshWatcher(self)
        self._watcher.start()

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()

    def _selected(
        self,
        names: list[str] | None = None,
        kinds: set[str] | None = None,
        require_available: bool = True,
    ) -> list[SourceConfig]:
        wanted = set(names or [])
        known = {source.name for source in self.sources}
        unknown = wanted - known
        if unknown:
            raise ValueError(f"unknown sources: {', '.join(sorted(unknown))}")
        out = []
        for source in self.sources:
            if not source.enabled:
                continue
            if wanted and source.name not in wanted:
                continue
            if kinds and source.kind not in kinds:
                continue
            if require_available and not source.path.exists():
                continue
            out.append(source)
        return out

    def status(self) -> dict[str, Any]:
        result = self.index.status(self.sources)
        checkpoints = self.archive.checkpoints()
        watcher = (
            self._watcher.snapshot()
            if self._watcher is not None
            else external_watcher_snapshot(self.settings)
            or {
                "enabled": self.settings.watch_enabled,
                "backend": "not-started",
                "healthy": False,
                "pending_sources": [],
                "stale_sources": [],
                "writer_lock": str(self.settings.sync_lock_path),
            }
        )
        pending = set(watcher.get("pending_sources") or [])
        watcher_stale = set(watcher.get("stale_sources") or [])
        by_name = {source.name: source for source in self.sources}
        for row in result.get("sources", []):
            source = by_name[str(row["name"])]
            checkpoint = checkpoints.get(source.name)
            catalog_only = source.kind == "skills" and source.state == "native"
            if not source.enabled or catalog_only:
                row["checkpoint"] = checkpoint or {}
                row["index_health"] = {
                    "current": True,
                    "reasons": [],
                    "lanes": row.get("lanes", {}),
                }
                row["managed"] = False
                row["management"] = (
                    "catalog-reference" if catalog_only else "disabled"
                )
                row["stale"] = False
                row["stale_reasons"] = []
                continue
            reasons: list[str] = []
            current_fingerprint = ""
            fingerprint_error = ""
            if row.get("available"):
                try:
                    current_fingerprint = source_fingerprint(source)
                except Exception as exc:
                    fingerprint_error = f"{type(exc).__name__}: {exc}"
                    reasons.append("fingerprint_failed")
            else:
                reasons.append("source_unavailable")
            if checkpoint is None:
                reasons.append("checkpoint_missing")
            else:
                if (
                    current_fingerprint
                    and current_fingerprint
                    != checkpoint.get("synced_fingerprint")
                ):
                    reasons.append("source_changed")
                if checkpoint.get("last_error"):
                    reasons.append("last_sync_failed")
            health = self.index.source_health(source, checkpoint)
            reasons.extend(health["reasons"])
            if source.name in pending:
                reasons.append("refresh_pending")
            if source.name in watcher_stale:
                reasons.append("watcher_marked_stale")
            row["checkpoint"] = {
                **(checkpoint or {}),
                "current_fingerprint": current_fingerprint,
                "fingerprint_error": fingerprint_error,
            }
            row["index_health"] = health
            row["managed"] = True
            row["stale"] = bool(reasons)
            row["stale_reasons"] = list(dict.fromkeys(reasons))
        result["archive"] = {
            "path": str(self.settings.archive_db),
            "sources": {
                source.name: {
                    "total": self.archive.count(source.name)[0],
                    "active": self.archive.count(source.name)[1],
                }
                for source in self.sources
            },
            "checkpoints": checkpoints,
        }
        result["watcher"] = watcher
        result["catalog"] = self.catalog.stats()
        result["projections"] = self.projections.list()
        return result

    def sync(
        self,
        names: list[str] | None = None,
        *,
        reason: str = "manual",
        only_if_stale: bool = False,
        lock_timeout: float | None = None,
    ) -> dict[str, Any]:
        configured_selected = self._selected(names=names)
        selected = [
            source
            for source in configured_selected
            if not (source.kind == "skills" and source.state == "native")
        ]
        unavailable = [
            source.name
            for source in self._selected(names=names, require_available=False)
            if not source.path.exists()
        ]
        self.settings.sync_lock_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = (
            self.settings.sync_lock_timeout
            if lock_timeout is None
            else max(0.0, lock_timeout)
        )
        lock = FileLock(str(self.settings.sync_lock_path))
        try:
            lock.acquire(timeout=timeout)
        except FileLockTimeout:
            return {
                "reason": reason,
                "synced": [],
                "skipped": [],
                "unavailable": unavailable,
                "lock": {
                    "path": str(self.settings.sync_lock_path),
                    "acquired": False,
                    "timeout": timeout,
                },
            }
        reports = []
        skipped = []
        catalog_report: dict[str, Any] | None = None
        pruned_unmanaged: list[dict[str, Any]] = []
        try:
            with self._write_mutex:
                pruned_unmanaged = self.index.prune_unmanaged(self.sources)
                approved_skill_ids: set[str] | None = set()
                if any(source.kind == "skills" for source in configured_selected):
                    try:
                        catalog_report = self.catalog.sync()
                        approved_skill_ids = set(self.catalog.entries())
                    except Exception as exc:
                        approved_skill_ids = None
                        catalog_report = {
                            "root": str(self.settings.catalog_root),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                skipped.extend(
                    {
                        "source": source.name,
                        "kind": source.kind,
                        "fingerprint": "",
                        "reason": "native_catalog_only",
                    }
                    for source in configured_selected
                    if source.kind == "skills" and source.state == "native"
                )
                for source in selected:
                    if source.kind == "skills" and approved_skill_ids is None:
                        skipped.append(
                            {
                                "source": source.name,
                                "kind": source.kind,
                                "fingerprint": "",
                                "reason": "catalog_unavailable",
                            }
                        )
                        continue
                    fingerprint = ""
                    try:
                        fingerprint = source_fingerprint(source)
                        checkpoint = self.archive.checkpoint(source.name)
                        health = self.index.source_health(source, checkpoint)
                        reasons = list(health["reasons"])
                        if checkpoint is None:
                            reasons.append("checkpoint_missing")
                        elif (
                            checkpoint.get("synced_fingerprint")
                            != fingerprint
                        ):
                            reasons.append("source_changed")
                        elif checkpoint.get("last_error"):
                            reasons.append("last_sync_failed")
                        if only_if_stale and not reasons:
                            skipped.append(
                                {
                                    "source": source.name,
                                    "kind": source.kind,
                                    "fingerprint": fingerprint,
                                    "reason": "current",
                                }
                            )
                            continue
                        current = list(iter_documents(source, self.settings))
                        if source.kind == "skills":
                            current = [
                                document
                                for document in current
                                if str(document.metadata.get("skill_id") or "")
                                in approved_skill_ids
                            ]
                        archive_report = self.archive.sync(
                            source,
                            current,
                            retain_history=source.kind
                            in {"context_mode", "hermes_sessions"},
                        )
                        archived = self.archive.documents_for_source(source.name)
                        index_report = self.index.sync_documents(
                            source,
                            archived,
                        )
                        fingerprint_after = source_fingerprint(source)
                        changed_during_sync = fingerprint_after != fingerprint
                        self.archive.mark_sync_success(
                            source,
                            fingerprint=fingerprint,
                            embedding_fingerprints=(
                                self.index.embedding_fingerprints
                            ),
                            document_count=len(archived),
                        )
                        index_report["archive"] = archive_report
                        index_report["fingerprint"] = fingerprint
                        index_report["changed_during_sync"] = (
                            changed_during_sync
                        )
                        reports.append(index_report)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        try:
                            self.archive.mark_sync_error(
                                source,
                                fingerprint=fingerprint,
                                error=error,
                            )
                        except Exception:
                            pass
                        reports.append(
                            {
                                "source": source.name,
                                "kind": source.kind,
                                "fingerprint": fingerprint,
                                "error": error,
                            }
                        )
        finally:
            lock.release()
        return {
            "reason": reason,
            "synced": reports,
            "skipped": skipped,
            "unavailable": unavailable,
            "catalog": catalog_report,
            "pruned_unmanaged": pruned_unmanaged,
            "lock": {
                "path": str(self.settings.sync_lock_path),
                "acquired": True,
                "timeout": timeout,
            },
        }

    def search_skills(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Fuse semantic Chroma ranking with IWE fuzzy/BM25 graph search."""

        query = query.strip()
        if not query:
            raise ValueError("search query must not be empty")
        limit = max(1, min(limit, 20))
        entries = self.catalog.entries()
        semantic = self.index.search(
            query,
            [
                source
                for source in self._selected(kinds={"skills"})
                if source.state in {"cold", "archived"}
            ],
            limit=max(limit * 8, 40),
        )
        iwe = self.catalog.find(
            query,
            limit=max(limit * 4, 20),
            states={"cold", "archived"},
        )
        ranked: dict[str, dict[str, Any]] = {}

        def row_for(item_id: str) -> dict[str, Any] | None:
            entry = entries.get(item_id)
            if not entry or entry.get("state") not in {"cold", "archived"}:
                return None
            return ranked.setdefault(
                item_id,
                {
                    "skill_id": item_id,
                    "name": entry["title"],
                    "description": entry.get("description", ""),
                    "repository": entry["source"],
                    "state": entry["state"],
                    "categories": entry.get("categories", []),
                    "path": entry["canonical_path"],
                    "rank_score": 0.0,
                    "semantic_rank": None,
                    "semantic_score": None,
                    "iwe_rank": None,
                },
            )

        seen_semantic: set[str] = set()
        semantic_rank = 0
        for hit in semantic:
            item_id = str(hit.metadata.get("skill_id") or "")
            if not item_id or item_id in seen_semantic:
                continue
            seen_semantic.add(item_id)
            row = row_for(item_id)
            if row is None:
                continue
            semantic_rank += 1
            row["semantic_rank"] = semantic_rank
            row["semantic_score"] = round(hit.score, 6)
            row["rank_score"] += 1.0 / (60 + semantic_rank)

        for match in iwe:
            item_id = str(match["item_id"])
            row = row_for(item_id)
            if row is None:
                continue
            iwe_rank = int(match["iwe_rank"])
            row["iwe_rank"] = iwe_rank
            row["rank_score"] += 1.0 / (60 + iwe_rank)

        normalized_query = query.casefold()
        query_terms = set(normalized_query.split())
        for row in ranked.values():
            name = str(row["name"]).casefold()
            if normalized_query == name:
                row["rank_score"] += 0.02
            elif query_terms and query_terms.issubset(set(name.split())):
                row["rank_score"] += 0.005

        rows = sorted(
            ranked.values(),
            key=lambda row: (-float(row["rank_score"]), str(row["name"]).casefold()),
        )
        deduped = []
        seen_paths: set[str] = set()
        seen_names: set[str] = set()
        peak = float(rows[0]["rank_score"]) if rows else 1.0
        for row in rows:
            canonical = str(Path(row["path"]).resolve(strict=False))
            name = str(row["name"]).strip().casefold()
            if canonical in seen_paths or name in seen_names:
                continue
            seen_paths.add(canonical)
            seen_names.add(name)
            row["score"] = round(float(row.pop("rank_score")) / peak, 6)
            deduped.append(row)
            if len(deduped) >= limit:
                break
        return {
            "query": query,
            "matches": deduped,
            "ranking": "Chroma semantic + IWE fuzzy/BM25 reciprocal-rank fusion",
        }

    def _scout_read(self, skill_id: str) -> dict[str, Any]:
        context = self.catalog.context(skill_id)
        entry = context["entry"]
        content = Path(entry["canonical_path"]).read_text(
            encoding="utf-8", errors="replace"
        )
        return {
            "skill_id": skill_id,
            "name": entry["title"],
            "description": entry.get("description", ""),
            "state": entry["state"],
            "categories": entry.get("categories", []),
            "canonical_excerpt": content[:12000],
            "iwe_graph": context["graph"],
        }

    def retrieve_skill(self, query: str) -> dict[str, Any]:
        """Delegate selection, return exact instructions, and project the package."""

        selection = self.scout.select(
            query,
            search=lambda value, limit: self.search_skills(value, limit)["matches"],
            read=self._scout_read,
        )
        skill_id = selection.get("selected_id")
        if not skill_id:
            return {
                "query": query,
                "selection": selection,
                "skill": None,
                "projection": None,
            }
        entry = self.catalog.entry(str(skill_id))
        if entry["state"] not in {"cold", "archived"}:
            raise RuntimeError("Retrieval Scout may project only cold or archived skills")
        canonical = Path(entry["canonical_path"])
        content = canonical.read_text(encoding="utf-8", errors="replace")
        projection = self.projections.project(entry)
        return {
            "query": query,
            "selection": selection,
            "skill": {
                "skill_id": skill_id,
                "name": entry["title"],
                "source": entry["source"],
                "state": entry["state"],
                "canonical_path": str(canonical),
                "content": content,
                "verbatim": True,
            },
            "projection": projection,
            "instruction": (
                "Use the returned SKILL.md immediately. The complete package is also "
                "projected for durable discovery; run /reload-skills in Hermes or "
                "/reload in OMP to refresh the current process without restarting."
            ),
        }

    def list_retrieved_skills(self) -> dict[str, Any]:
        return self.projections.list()

    def clear_retrieved_skills(
        self,
        skill_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.projections.clear(skill_ids)

    def find_skills(self, query: str, limit: int = 8) -> dict[str, Any]:
        result = self.search_skills(query, limit)
        result["instruction"] = (
            "Compatibility search only. MCP callers should use retrieve_skill, which "
            "delegates selection and projects one complete skill safely."
        )
        return result

    def load_skills(self, skill_ids: list[str]) -> dict[str, Any]:
        requested = list(dict.fromkeys(skill_ids))
        if not requested:
            raise ValueError("at least one skill ID is required")
        if len(requested) > self.settings.max_skills_per_load:
            raise ValueError(
                f"at most {self.settings.max_skills_per_load} skills may be loaded at once"
            )
        catalog = skill_catalog(self.sources)
        missing = [skill_id for skill_id in requested if skill_id not in catalog]
        if missing:
            raise ValueError(f"unknown skill IDs: {', '.join(missing)}")
        loaded = []
        total = 0
        for skill_id in requested:
            source, path = catalog[skill_id]
            bundle_paths, resources = skill_bundle_files(source, path)
            files = []
            skill_total = 0
            truncated = False
            for bundle_path in bundle_paths:
                content = bundle_path.read_text(encoding="utf-8", errors="replace")
                per_skill_remaining = self.settings.max_skill_chars - skill_total
                global_remaining = self.settings.max_total_skill_chars - total
                remaining = min(per_skill_remaining, global_remaining)
                if remaining <= 0:
                    truncated = True
                    break
                if len(content) > remaining:
                    content = content[:remaining]
                    truncated = True
                skill_total += len(content)
                total += len(content)
                try:
                    relative_path = bundle_path.relative_to(path.parent).as_posix()
                except ValueError:
                    relative_path = bundle_path.relative_to(source.path).as_posix()
                files.append(
                    {
                        "path": str(bundle_path),
                        "relative_path": relative_path,
                        "content": content,
                    }
                )
                if truncated:
                    break
            loaded.append(
                {
                    "skill_id": skill_id,
                    "repository": source.name,
                    "path": str(path),
                    "files": files,
                    "resources": resources,
                    "chars": skill_total,
                    "truncated": truncated,
                }
            )
        return {"skills": loaded, "total_chars": total}

    def find_workflows(
        self,
        query: str,
        workflow_types: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 20))
        allowed_types = {"agent", "command", "hook"}
        requested_types = set(workflow_types or [])
        unknown = requested_types - allowed_types
        if unknown:
            raise ValueError(
                f"unknown workflow types: {', '.join(sorted(unknown))}"
            )
        hits = self.index.search(
            query,
            self._selected(kinds={"workflows"}),
            limit=max(limit * 5, limit),
        )
        rows = []
        seen: set[str] = set()
        for hit in hits:
            workflow_id = str(hit.metadata.get("workflow_id") or "")
            workflow_type = str(hit.metadata.get("workflow_type") or "")
            if not workflow_id or workflow_id in seen:
                continue
            if requested_types and workflow_type not in requested_types:
                continue
            seen.add(workflow_id)
            rows.append(
                {
                    "workflow_id": workflow_id,
                    "type": workflow_type,
                    "name": hit.title,
                    "description": str(hit.metadata.get("description") or ""),
                    "repository": hit.source_name,
                    "path": hit.locator,
                    "score": round(hit.score, 4),
                    "lane": hit.lane,
                }
            )
            if len(rows) >= limit:
                break
        return {
            "query": query,
            "types": sorted(requested_types),
            "matches": rows,
            "instruction": (
                "Call load_workflows only for selected workflow IDs. Loading a hook "
                "returns its source; it does not install or activate the hook."
            ),
        }

    def load_workflows(self, workflow_ids: list[str]) -> dict[str, Any]:
        requested = list(dict.fromkeys(workflow_ids))
        if not requested:
            raise ValueError("at least one workflow ID is required")
        if len(requested) > self.settings.max_skills_per_load:
            raise ValueError(
                f"at most {self.settings.max_skills_per_load} workflows may be loaded at once"
            )
        catalog = workflow_catalog(self.sources)
        missing = [workflow_id for workflow_id in requested if workflow_id not in catalog]
        if missing:
            raise ValueError(f"unknown workflow IDs: {', '.join(missing)}")
        loaded = []
        total = 0
        for workflow_id in requested:
            source, path, workflow_type = catalog[workflow_id]
            content = path.read_text(encoding="utf-8", errors="replace")
            remaining = min(
                self.settings.max_skill_chars,
                self.settings.max_total_skill_chars - total,
            )
            truncated = len(content) > max(0, remaining)
            content = content[: max(0, remaining)]
            total += len(content)
            loaded.append(
                {
                    "workflow_id": workflow_id,
                    "type": workflow_type,
                    "repository": source.name,
                    "path": str(path),
                    "content": content,
                    "chars": len(content),
                    "truncated": truncated,
                    "activation": "manual",
                }
            )
        return {"workflows": loaded, "total_chars": total}

    def recall(
        self,
        query: str,
        sources: list[str] | None = None,
        limit: int = 8,
        before: int = 2,
        after: int = 3,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 20))
        selected = self._selected(
            names=sources,
            kinds={"context_mode", "hermes_sessions"},
        )
        hits = self.index.search(query, selected, limit=limit)
        remaining = self.settings.max_recall_chars
        rows = []
        seen_context: set[str] = set()
        for hit in hits:
            if remaining <= 0:
                break
            timeline = []
            neighbors = self.archive.neighbors(
                hit.record_id,
                before=max(0, min(before, 10)),
                after=max(0, min(after, 10)),
            )
            for item in neighbors:
                if item.record_id in seen_context or remaining <= 0:
                    continue
                seen_context.add(item.record_id)
                excerpt = item.content[: min(1200, remaining)]
                remaining -= len(excerpt)
                timeline.append(
                    {
                        "record_id": item.record_id,
                        "locator": item.locator,
                        "excerpt": excerpt,
                        "role": str(item.metadata.get("role") or ""),
                        "tool_name": str(item.metadata.get("tool_name") or ""),
                        "tool_call_id": str(
                            item.metadata.get("tool_call_id") or ""
                        ),
                        "timestamp": item.metadata.get("timestamp", ""),
                        "profile": str(item.metadata.get("profile") or ""),
                        "session_id": str(
                            item.metadata.get("session_id") or ""
                        ),
                        "sequence": item.metadata.get("sequence"),
                        "message_position": item.metadata.get("message_position"),
                        "rowid": item.metadata.get("rowid"),
                    }
                )
            rows.append(
                {
                    "source": hit.source_name,
                    "kind": hit.kind,
                    "title": hit.title,
                    "locator": hit.locator,
                    "matched_excerpt": hit.content[:1200],
                    "timeline": timeline,
                    "score": round(hit.score, 4),
                    "lane": hit.lane,
                }
            )
        return {"query": query, "matches": rows}
