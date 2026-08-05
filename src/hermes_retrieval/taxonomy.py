from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any


_CATEGORY_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _terms(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    version: int
    categories: tuple[Category, ...]

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(category.id for category in self.categories)

    def validate(self, category_ids: list[str], *, owner: str) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in category_ids if item.strip()))
        unknown = sorted(set(normalized) - self.ids)
        if unknown:
            raise ValueError(
                f"{owner} names unknown Retrieval categories: {', '.join(unknown)}"
            )
        return normalized

    def classify(self, haystack: str) -> list[str]:
        folded = haystack.casefold()
        return [
            category.id
            for category in self.categories
            if any(keyword in folded for keyword in category.keywords)
        ][:8]


def load_taxonomy(path: Path) -> Taxonomy:
    if not path.is_file():
        raise FileNotFoundError(f"Retrieval taxonomy is missing: {path}")
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != 1:
        raise ValueError(f"unsupported Retrieval taxonomy version: {version!r}")
    categories: list[Category] = []
    seen: set[str] = set()
    for position, row in enumerate(payload.get("categories", []), start=1):
        if not isinstance(row, dict):
            raise ValueError(f"taxonomy category {position} must be a table")
        category_id = str(row.get("id") or "").strip()
        if not _CATEGORY_ID.fullmatch(category_id):
            raise ValueError(f"invalid Retrieval category ID: {category_id!r}")
        if category_id in seen:
            raise ValueError(f"duplicate Retrieval category ID: {category_id}")
        label = str(row.get("label") or "").strip()
        if not label:
            raise ValueError(f"Retrieval category {category_id} has no label")
        keywords = tuple(
            dict.fromkeys(term.casefold() for term in _terms(row.get("keywords")))
        )
        if not keywords:
            raise ValueError(f"Retrieval category {category_id} has no keywords")
        categories.append(Category(category_id, label, keywords))
        seen.add(category_id)
    if not categories:
        raise ValueError("Retrieval taxonomy contains no categories")
    return Taxonomy(version=version, categories=tuple(categories))


def load_category_overrides(path: Path, taxonomy: Taxonomy) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("skills", {})
    if not isinstance(raw, dict):
        raise ValueError("category-overrides.toml [skills] must be a table")
    overrides: dict[str, list[str]] = {}
    for item_id, value in raw.items():
        key = str(item_id).strip()
        if not key:
            raise ValueError("category override IDs must not be empty")
        categories = taxonomy.validate(
            _terms(value), owner=f"category override {key!r}"
        )
        if not categories:
            raise ValueError(f"category override {key!r} has no categories")
        overrides[key] = categories
    return overrides
