from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterable

import yaml

from .config import Settings
from .models import SourceConfig
from .sources import iter_skills
from .taxonomy import Taxonomy, load_category_overrides, load_taxonomy


_MANIFEST = ".retrieval-catalog.json"
_MANIFEST_VERSION = 2
_STATE_PRIORITY = {"archived": 1, "cold": 2, "hidden": 3, "native": 4}


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


class IweCatalog:
    """Disposable IWE graph derived from explicitly configured skill sources."""

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

    def _iwe(self) -> str:
        configured = os.path.expanduser(self.settings.iwe_command)
        if os.path.sep in configured:
            path = Path(configured)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        found = shutil.which(configured)
        if found:
            return found
        raise RuntimeError(
            "IWE is not installed; run cargo install iwe iwes --locked "
            "or set RETRIEVAL_IWE_COMMAND"
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
                candidates.append({
                    "item_id": item_id,
                    "kind": "skill",
                    "title": document.title,
                    "description": str(document.metadata.get("description") or ""),
                    "source": source.name,
                    "state": str(document.metadata.get("state") or source.state),
                    "canonical_path": canonical_path,
                    "relative_path": relative_path,
                    "categories": categories,
                    "tags": tags,
                    "card": card,
                    "iwe_key": card[:-3],
                    "descriptor": document.content,
                })

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
            if current is None or prefer(entry, current):
                by_canonical[key] = entry

        by_title: dict[str, dict[str, Any]] = {}
        for entry in by_canonical.values():
            key = " ".join(str(entry["title"]).casefold().split())
            current = by_title.get(key)
            if current is None or prefer(entry, current):
                by_title[key] = entry

        native_excluded = sum(
            entry["state"] == "native" for entry in by_title.values()
        )
        dormant = {
            str(entry["item_id"]): entry
            for entry in by_title.values()
            if entry["state"] in {"hidden", "cold", "archived"}
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
            "duplicates_excluded": len(candidates) - len(by_title),
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
        probe = IweCatalog(self.settings, [*native, candidate])
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
            "duplicates_excluded": inventory["duplicates_excluded"],
            "owned_files": sorted(owned),
        }
        changed += int(
            _atomic_text(
                self.manifest_path,
                json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            )
        )

        initialized = False
        if not (self.root / ".iwe" / "config.toml").is_file():
            result = subprocess.run(
                [self._iwe(), "init", "--auto", "--library", "."],
                cwd=self.root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if result.returncode not in {0, 2}:
                raise RuntimeError(
                    f"IWE initialization failed ({result.returncode}): "
                    f"{(result.stderr or result.stdout)[-1200:]}"
                )
            initialized = result.returncode == 0
        return {
            "root": str(self.root),
            "entries": len(entries),
            "review_required": len(review),
            "native_excluded": native_excluded,
            "duplicates_excluded": inventory["duplicates_excluded"],
            "hidden": sum(row["state"] == "hidden" for row in entries.values()),
            "cold": sum(row["state"] == "cold" for row in entries.values()),
            "archived": sum(row["state"] == "archived" for row in entries.values()),
            "files_changed": changed,
            "files_removed": removed,
            "iwe_initialized": initialized,
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
        result = subprocess.run(
            [
                self._iwe(),
                "find",
                "--fuzzy",
                query,
                "--lexical",
                query,
                "--limit",
                str(min(250, limit * 8)),
                "--format",
                "json",
            ],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"IWE search failed ({result.returncode}): {result.stderr[-1200:]}"
            )
        try:
            found = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise RuntimeError("IWE returned invalid JSON") from exc
        by_key = {row["iwe_key"]: row for row in self.entries().values()}
        allowed = states or {"hidden", "cold", "archived"}
        rows: list[dict[str, Any]] = []
        for rank, match in enumerate(found, start=1):
            if not isinstance(match, dict):
                continue
            entry = by_key.get(str(match.get("key") or ""))
            if not entry or entry["state"] not in allowed:
                continue
            rows.append({**entry, "iwe_rank": rank})
            if len(rows) >= limit:
                break
        return rows

    def context(self, item_id: str) -> dict[str, Any]:
        entry = self.entry(item_id)
        result = subprocess.run(
            [
                self._iwe(),
                "retrieve",
                "--key",
                entry["iwe_key"],
                "--expand-included-by",
                "1",
                "--expand-references",
                "1",
                "--max-documents",
                "8",
                "--max-tokens",
                "3000",
                "--max-document-tokens",
                "1200",
                "--format",
                "json",
            ],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"IWE retrieval failed ({result.returncode}): {result.stderr[-1200:]}"
            )
        try:
            graph = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise RuntimeError("IWE returned invalid graph JSON") from exc
        return {"entry": entry, "graph": graph}

    def stats(self) -> dict[str, Any]:
        manifest = self.manifest()
        entries = list(manifest.get("entries", {}).values())
        review = list(manifest.get("review", {}).values())
        iwe: dict[str, Any]
        try:
            command = self._iwe()
            result = subprocess.run(
                [command, "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            iwe = {
                "command": command,
                "available": result.returncode == 0,
                "version": (result.stdout or result.stderr).strip(),
                "integration": "external-cli",
                "auto_update": False,
            }
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            iwe = {
                "command": self.settings.iwe_command,
                "available": False,
                "version": "",
                "integration": "external-cli",
                "auto_update": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "root": str(self.root),
            "initialized": (self.root / ".iwe" / "config.toml").is_file(),
            "entries": len(entries),
            "review_required": len(review),
            "native_excluded": int(manifest.get("native_excluded") or 0),
            "duplicates_excluded": int(manifest.get("duplicates_excluded") or 0),
            "states": {
                state: sum(row.get("state") == state for row in entries)
                for state in ("native", "hidden", "cold", "archived")
            },
            "iwe": iwe,
            "generated_at": manifest.get("generated_at", ""),
        }
