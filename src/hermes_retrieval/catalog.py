from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

import yaml

from .config import Settings
from .graph_index import IntegratedGraphIndex, catalog_tree
from .models import SourceConfig
from .sources import iter_skills
from .taxonomy import Taxonomy, load_category_overrides, load_taxonomy


_MANIFEST = ".retrieval-catalog.json"
_MANIFEST_VERSION = 5
_STATE_PRIORITY = {"archived": 1, "cold": 2, "hidden": 3, "native": 4}
_BUNDLE_IGNORES = {
    ".git",
    ".retrieval-intake.json",
    ".retrieval-projection.json",
    "__pycache__",
    "node_modules",
}


def _slug(value: str, fallback: str = "item") -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return clean[:80] or fallback


def _frontmatter_payload(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    try:
        payload = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_terms(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;/]", value) if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _classification(
    *,
    item_id: str,
    source: SourceConfig,
    title: str,
    relative_path: str,
    description: str,
    text: str,
    taxonomy: Taxonomy,
    overrides: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    payload = _frontmatter_payload(text)
    explicit: list[str] = []
    for key in ("category", "categories", "tag", "tags"):
        explicit.extend(_as_terms(payload.get(key)))
    tags = list(
        dict.fromkeys(_slug(item, "uncategorized") for item in explicit)
    )[:12]
    if item_id in overrides:
        return list(overrides[item_id]), tags
    declared = _as_terms(payload.get("retrieval_categories"))
    if declared:
        return taxonomy.validate(
            declared,
            owner=f"SKILL.md retrieval_categories for {item_id!r}",
        )[:8], tags
    haystack = " ".join(
        (source.name, title, relative_path, description, text[:5000])
    )
    return taxonomy.classify(haystack), tags


def _safe_owned_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=False)
    candidate.relative_to(root.resolve())
    return candidate


def _atomic_text(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def bundle_digest(skill_path: Path) -> str:
    """Hash a complete skill package without following links outside it."""

    root = skill_path.parent.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root)
        if any(part in _BUNDLE_IGNORES for part in relative.parts):
            continue
        if path.is_symlink():
            digest.update(b"L\0")
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            continue
        if not path.is_file():
            continue
        digest.update(b"F\0")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while piece := stream.read(1024 * 1024):
                digest.update(piece)
        digest.update(b"\0")
    return digest.hexdigest()


class RetrievalCatalog:
    """Disposable integrated graph derived from configured skill sources."""

    def __init__(self, settings: Settings, sources: Iterable[SourceConfig]):
        self.settings = settings
        self.sources = list(sources)
        self.root = settings.catalog_root
        self.manifest_path = self.root / _MANIFEST

    def _policy(self) -> tuple[Taxonomy, dict[str, list[str]]]:
        # Policy is deliberately reloaded on every administrative/watcher sync.
        # A human can review an intake, edit an override, and sync without
        # restarting a long-lived watcher process.
        taxonomy = load_taxonomy(self.settings.taxonomy_file)
        return taxonomy, load_category_overrides(
            self.settings.category_overrides_file,
            taxonomy,
        )

    def manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {
                "version": _MANIFEST_VERSION,
                "entries": {},
                "review": {},
                "owned_files": [],
            }
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _MANIFEST_VERSION
            or not isinstance(payload.get("entries"), dict)
        ):
            return {
                "version": _MANIFEST_VERSION,
                "entries": {},
                "review": {},
                "owned_files": [],
            }
        return payload

    def _inventory(self) -> dict[str, Any]:
        taxonomy, overrides = self._policy()
        candidates: list[dict[str, Any]] = []
        for source in self.sources:
            if not source.enabled or source.kind != "skills" or not source.path.is_dir():
                continue
            for document in iter_skills(source):
                item_id = str(document.metadata["skill_id"])
                canonical_path = str(Path(document.locator).resolve())
                text = Path(canonical_path).read_text(
                    encoding="utf-8", errors="replace"
                )
                relative_path = str(document.metadata.get("relative_path") or "")
                categories, tags = _classification(
                    item_id=item_id,
                    source=source,
                    title=document.title,
                    relative_path=relative_path,
                    description=str(document.metadata.get("description") or ""),
                    text=text,
                    taxonomy=taxonomy,
                    overrides=overrides,
                )
                stem = _slug(document.title, _slug(Path(canonical_path).parent.name))
                digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:8]
                card = f"skills/{_slug(source.name)}/{stem}-{digest}.md"
                package_hash = bundle_digest(Path(canonical_path))
                state = str(document.metadata.get("state") or source.state)
                native_harnesses = (
                    [source.harness]
                    if state == "native" and source.harness
                    else []
                )
                hidden_harnesses = (
                    [source.harness]
                    if state == "hidden" and source.harness
                    else []
                )
                candidates.append({
                    "item_id": item_id,
                    "kind": "skill",
                    "title": document.title,
                    "description": str(document.metadata.get("description") or ""),
                    "source": source.name,
                    "state": state,
                    "native_harness": str(
                        document.metadata.get("native_harness") or source.harness
                    ),
                    "native_harnesses": native_harnesses,
                    "hidden_harnesses": hidden_harnesses,
                    "canonical_path": canonical_path,
                    "relative_path": relative_path,
                    "categories": categories,
                    "tags": tags,
                    "card": card,
                    "graph_key": card[:-3],
                    "package_hash": package_hash,
                    "duplicate_paths": [canonical_path],
                    "duplicate_sources": [source.name],
                    "duplicate_item_ids": [item_id],
                    "descriptor": document.content,
                })

        def merge_harnesses(
            preferred: dict[str, Any],
            other: dict[str, Any],
        ) -> dict[str, Any]:
            merged = dict(preferred)
            for key in ("native_harnesses", "hidden_harnesses"):
                merged[key] = sorted(
                    {
                        str(value)
                        for value in [
                            *(preferred.get(key) or []),
                            *(other.get(key) or []),
                        ]
                        if str(value) in {"hermes", "omp"}
                    }
                )
            for key in ("duplicate_paths", "duplicate_sources", "duplicate_item_ids"):
                merged[key] = sorted(
                    {
                        str(value)
                        for value in [
                            *(preferred.get(key) or []),
                            *(other.get(key) or []),
                        ]
                        if str(value)
                    }
                )
            return merged

        def prefer(new: dict[str, Any], current: dict[str, Any]) -> bool:
            new_priority = _STATE_PRIORITY[str(new["state"])]
            current_priority = _STATE_PRIORITY[str(current["state"])]
            if new_priority != current_priority:
                return new_priority > current_priority
            new_path = str(new.get("relative_path") or "")
            current_path = str(current.get("relative_path") or "")
            return (
                len(Path(new_path).parts),
                len(new_path),
                str(new["item_id"]),
            ) < (
                len(Path(current_path).parts),
                len(current_path),
                str(current["item_id"]),
            )

        by_canonical: dict[str, dict[str, Any]] = {}
        for entry in candidates:
            key = str(entry["canonical_path"])
            current = by_canonical.get(key)
            if current is None:
                by_canonical[key] = entry
            elif prefer(entry, current):
                by_canonical[key] = merge_harnesses(entry, current)
            else:
                by_canonical[key] = merge_harnesses(current, entry)

        # Merge only byte-identical packages. Two skills with the same display
        # name but different contents remain separately addressable.
        by_package: dict[str, dict[str, Any]] = {}
        for entry in by_canonical.values():
            key = str(entry["package_hash"])
            current = by_package.get(key)
            if current is None:
                by_package[key] = entry
            elif prefer(entry, current):
                by_package[key] = merge_harnesses(entry, current)
            else:
                by_package[key] = merge_harnesses(current, entry)

        def fully_native(entry: dict[str, Any]) -> bool:
            harnesses = set(entry.get("native_harnesses") or [])
            if entry["state"] == "native" and not harnesses:
                # A legacy/native source without provenance is conservatively
                # treated as visible in both harnesses.
                return True
            return harnesses >= {"hermes", "omp"}

        native_excluded = sum(fully_native(entry) for entry in by_package.values())
        native_by_harness = {
            harness: sum(
                harness in set(entry.get("native_harnesses") or [])
                for entry in by_package.values()
            )
            for harness in ("hermes", "omp")
        }
        dormant = {
            str(entry["item_id"]): entry
            for entry in by_package.values()
            if not fully_native(entry)
            and entry["state"] in {"native", "hidden", "cold", "archived"}
        }
        entries = {
            item_id: entry
            for item_id, entry in dormant.items()
            if entry["categories"]
        }
        review = {
            item_id: {
                **entry,
                "review_reason": "no-approved-category",
            }
            for item_id, entry in dormant.items()
            if not entry["categories"]
        }
        return {
            "entries": dict(sorted(entries.items())),
            "review": dict(sorted(review.items())),
            "native_excluded": native_excluded,
            "native_by_harness": native_by_harness,
            "duplicates_excluded": len(candidates) - len(by_package),
        }

    def audit(self) -> dict[str, Any]:
        inventory = self._inventory()
        entries = inventory["entries"]
        review = inventory["review"]
        category_counts = Counter(
            category
            for entry in entries.values()
            for category in entry["categories"]
        )
        source_counts = Counter(entry["source"] for entry in entries.values())
        return {
            "total": len(entries) + len(review),
            "approved": len(entries),
            "review_required": len(review),
            "native_excluded": inventory["native_excluded"],
            "native_by_harness": inventory["native_by_harness"],
            "duplicates_excluded": inventory["duplicates_excluded"],
            "categories": dict(sorted(category_counts.items())),
            "sources": dict(sorted(source_counts.items())),
            "review": [
                {
                    "skill_id": item_id,
                    "name": entry["title"],
                    "source": entry["source"],
                    "path": entry["canonical_path"],
                    "tags": entry["tags"],
                    "reason": entry["review_reason"],
                }
                for item_id, entry in review.items()
            ],
        }

    def audit_path(self, path: Path, name: str) -> dict[str, Any]:
        candidate_path = path.expanduser().resolve()
        if not candidate_path.is_dir():
            raise ValueError(f"skill intake path is not a directory: {candidate_path}")
        if not any(candidate_path.rglob("SKILL.md")):
            raise ValueError(f"skill intake path contains no SKILL.md: {candidate_path}")
        candidate = SourceConfig(
            name=name,
            kind="skills",
            path=candidate_path,
            enabled=True,
            state="cold",
        )
        native = [
            source
            for source in self.sources
            if source.enabled and source.kind == "skills" and source.state == "native"
        ]
        probe = RetrievalCatalog(self.settings, [*native, candidate])
        inventory = probe._inventory()
        entries = {
            item_id: entry
            for item_id, entry in inventory["entries"].items()
            if entry["source"] == name
        }
        review = {
            item_id: entry
            for item_id, entry in inventory["review"].items()
            if entry["source"] == name
        }
        report = {
            "total": len(entries) + len(review),
            "approved": len(entries),
            "review_required": len(review),
            "native_excluded": inventory["native_excluded"],
            "duplicates_excluded": inventory["duplicates_excluded"],
            "categories": dict(
                sorted(
                    Counter(
                        category
                        for entry in entries.values()
                        for category in entry["categories"]
                    ).items()
                )
            ),
            "sources": {name: len(entries)},
            "review": [
                {
                    "skill_id": item_id,
                    "name": entry["title"],
                    "source": entry["source"],
                    "path": entry["canonical_path"],
                    "tags": entry["tags"],
                    "reason": entry["review_reason"],
                }
                for item_id, entry in review.items()
            ],
        }
        report["path"] = str(candidate_path)
        report["name"] = name
        return report

    @staticmethod
    def _card(entry: dict[str, Any]) -> str:
        metadata = {
            "title": entry["title"],
            "item_id": entry["item_id"],
            "kind": "skill",
            "source": entry["source"],
            "state": entry["state"],
            "native_harness": entry.get("native_harness", ""),
            "native_harnesses": entry.get("native_harnesses", []),
            "hidden_harnesses": entry.get("hidden_harnesses", []),
            "categories": entry["categories"],
            "tags": entry["tags"],
            "canonical_path": entry["canonical_path"],
        }
        front = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        description = entry["description"] or "No explicit description supplied."
        return (
            f"---\n{front}\n---\n\n"
            f"# {entry['title']}\n\n"
            f"{description}\n\n"
            f"{entry['descriptor']}\n"
        )

    @staticmethod
    def _hub(title: str, links: list[tuple[str, str]]) -> str:
        body = [f"# {title}", ""]
        for label, target in links:
            body.extend((f"[{label}]({target})", ""))
        return "\n".join(body).rstrip() + "\n"

    def sync(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        previous = self.manifest()
        inventory = self._inventory()
        entries = inventory["entries"]
        review = inventory["review"]
        native_excluded = inventory["native_excluded"]
        owned: set[str] = set()
        changed = 0

        for entry in entries.values():
            owned.add(entry["card"])
            changed += int(
                _atomic_text(self.root / entry["card"], self._card(entry))
            )

        source_groups: dict[str, list[dict[str, Any]]] = {}
        category_groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries.values():
            source_groups.setdefault(entry["source"], []).append(entry)
            for category in entry["categories"]:
                category_groups.setdefault(category, []).append(entry)

        source_hubs: list[tuple[str, str]] = []
        for source_name, rows in sorted(source_groups.items()):
            relative = f"sources/{_slug(source_name)}.md"
            owned.add(relative)
            source_hubs.append((source_name, relative))
            links = [
                (row["title"], f"../{row['card']}")
                for row in sorted(rows, key=lambda item: item["title"].casefold())
            ]
            changed += int(
                _atomic_text(
                    self.root / relative,
                    self._hub(f"Source · {source_name}", links),
                )
            )

        category_hubs: list[tuple[str, str]] = []
        for category, rows in sorted(category_groups.items()):
            relative = f"categories/{_slug(category)}.md"
            owned.add(relative)
            category_hubs.append((category.replace("-", " ").title(), relative))
            links = [
                (row["title"], f"../{row['card']}")
                for row in sorted(rows, key=lambda item: item["title"].casefold())
            ]
            changed += int(
                _atomic_text(
                    self.root / relative,
                    self._hub(f"Category · {category.replace('-', ' ').title()}", links),
                )
            )

        index_links = [
            ("Browse by source", "sources.md"),
            ("Browse by category", "categories.md"),
        ]
        changed += int(
            _atomic_text(self.root / "index.md", self._hub("Retrieval catalog", index_links))
        )
        owned.add("index.md")
        changed += int(
            _atomic_text(
                self.root / "sources.md",
                self._hub("Sources", source_hubs),
            )
        )
        owned.add("sources.md")
        changed += int(
            _atomic_text(
                self.root / "categories.md",
                self._hub("Categories", category_hubs),
            )
        )
        owned.add("categories.md")

        removed = 0
        for relative in set(previous.get("owned_files") or []) - owned:
            target = _safe_owned_path(self.root, str(relative))
            if target.is_file():
                target.unlink()
                removed += 1
        for directory_name in ("skills", "sources", "categories"):
            base = self.root / directory_name
            if base.is_dir():
                for directory in sorted(
                    (path for path in base.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass

        manifest = {
            "version": _MANIFEST_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
            "review": review,
            "native_excluded": native_excluded,
            "native_by_harness": inventory["native_by_harness"],
            "duplicates_excluded": inventory["duplicates_excluded"],
            "owned_files": sorted(owned),
        }
        changed += int(
            _atomic_text(
                self.manifest_path,
                json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            )
        )

        return {
            "root": str(self.root),
            "entries": len(entries),
            "review_required": len(review),
            "native_excluded": native_excluded,
            "native_by_harness": inventory["native_by_harness"],
            "duplicates_excluded": inventory["duplicates_excluded"],
            "hidden": sum(row["state"] == "hidden" for row in entries.values()),
            "cold": sum(row["state"] == "cold" for row in entries.values()),
            "archived": sum(row["state"] == "archived" for row in entries.values()),
            "files_changed": changed,
            "files_removed": removed,
            "graph_index": "integrated-bm25-fuzzy-rrf",
        }

    def ensure(self) -> None:
        if not self.manifest_path.is_file():
            self.sync()

    def entry(self, item_id: str) -> dict[str, Any]:
        self.ensure()
        row = self.manifest().get("entries", {}).get(item_id)
        if not isinstance(row, dict):
            raise ValueError(f"unknown skill ID: {item_id}")
        return dict(row)

    def entries(self) -> dict[str, dict[str, Any]]:
        self.ensure()
        return {
            str(key): dict(value)
            for key, value in self.manifest().get("entries", {}).items()
            if isinstance(value, dict)
        }

    def find(
        self,
        query: str,
        *,
        limit: int = 12,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure()
        query = query.strip()
        if not query:
            raise ValueError("search query must not be empty")
        limit = max(1, min(limit, 50))
        allowed = states or {"hidden", "cold", "archived"}
        entries = {
            item_id: row
            for item_id, row in self.entries().items()
            if row.get("state") in allowed
        }
        ranked = IntegratedGraphIndex(entries.values()).rank(
            query,
            limit=min(250, limit * 8),
        )
        return [
            {**entries[item_id], "graph_rank": rank}
            for rank, item_id in enumerate(ranked[:limit], start=1)
        ]

    def context(self, item_id: str) -> dict[str, Any]:
        entry = self.entry(item_id)
        graph = IntegratedGraphIndex(self.entries().values()).context(item_id)
        return {"entry": entry, "graph": graph}

    def browse(self) -> dict[str, Any]:
        self.ensure()
        manifest = self.manifest()
        return catalog_tree(
            (
                dict(value)
                for value in manifest.get("entries", {}).values()
                if isinstance(value, dict)
            ),
            (
                dict(value)
                for value in manifest.get("review", {}).values()
                if isinstance(value, dict)
            ),
        )

    def stats(self) -> dict[str, Any]:
        manifest = self.manifest()
        entries = list(manifest.get("entries", {}).values())
        review = list(manifest.get("review", {}).values())
        return {
            "root": str(self.root),
            "initialized": self.manifest_path.is_file(),
            "entries": len(entries),
            "review_required": len(review),
            "native_excluded": int(manifest.get("native_excluded") or 0),
            "native_by_harness": dict(manifest.get("native_by_harness") or {}),
            "duplicates_excluded": int(manifest.get("duplicates_excluded") or 0),
            "states": {
                state: sum(row.get("state") == state for row in entries)
                for state in ("native", "hidden", "cold", "archived")
            },
            "graph_index": {
                "available": True,
                "integration": "in-process",
                "ranking": "BM25 + fuzzy subsequence + reciprocal-rank fusion",
                "relationships": ["source", "category", "tag", "state"],
                "external_runtime": False,
            },
            "generated_at": manifest.get("generated_at", ""),
        }
