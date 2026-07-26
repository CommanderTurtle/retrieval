from __future__ import annotations

from typing import Any

from .config import Settings
from .index import RetrievalIndex
from .models import SourceConfig
from .sources import skill_catalog


class RetrievalService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.sources = self.settings.sources()
        self.index = RetrievalIndex(self.settings)

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
        return self.index.status(self.sources)

    def sync(self, names: list[str] | None = None) -> dict[str, Any]:
        selected = self._selected(names=names)
        reports = []
        for source in selected:
            try:
                reports.append(self.index.sync(source))
            except Exception as exc:
                reports.append(
                    {
                        "source": source.name,
                        "kind": source.kind,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        unavailable = [
            source.name
            for source in self._selected(names=names, require_available=False)
            if not source.path.exists()
        ]
        return {"synced": reports, "unavailable": unavailable}

    def find_skills(self, query: str, limit: int = 8) -> dict[str, Any]:
        limit = max(1, min(limit, 20))
        hits = self.index.search(
            query,
            self._selected(kinds={"skills"}),
            limit=max(limit * 3, limit),
        )
        rows = []
        seen: set[str] = set()
        for hit in hits:
            skill_id = str(hit.metadata.get("skill_id") or "")
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            rows.append(
                {
                    "skill_id": skill_id,
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
            "matches": rows,
            "instruction": "Call load_skills only for the selected skill IDs; multiple IDs are supported.",
        }

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
            content = path.read_text(encoding="utf-8", errors="replace")
            truncated = False
            if len(content) > self.settings.max_skill_chars:
                content = content[: self.settings.max_skill_chars]
                truncated = True
            remaining = self.settings.max_total_skill_chars - total
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[:remaining]
                truncated = True
            total += len(content)
            loaded.append(
                {
                    "skill_id": skill_id,
                    "repository": source.name,
                    "path": str(path),
                    "content": content,
                    "truncated": truncated,
                }
            )
        return {"skills": loaded, "total_chars": total}

    def recall(
        self,
        query: str,
        sources: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 20))
        selected = self._selected(
            names=sources,
            kinds={"context_mode", "hermes_sessions", "librarian"},
        )
        hits = self.index.search(query, selected, limit=limit)
        remaining = self.settings.max_recall_chars
        rows = []
        for hit in hits:
            if remaining <= 0:
                break
            excerpt = hit.content[: min(1200, remaining)]
            remaining -= len(excerpt)
            rows.append(
                {
                    "source": hit.source_name,
                    "kind": hit.kind,
                    "title": hit.title,
                    "locator": hit.locator,
                    "excerpt": excerpt,
                    "score": round(hit.score, 4),
                    "lane": hit.lane,
                }
            )
        return {"query": query, "matches": rows}

