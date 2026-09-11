"""Integrated lexical/fuzzy index and bounded skill graph.

Retrieval owns BM25 title/body ranking, fuzzy identity matching, reciprocal-rank
fusion, and source/category/tag traversal in-process. No companion checkout,
executable, process, or configuration is required.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import re
import unicodedata
from typing import Any, Iterable


_K1 = 1.2
_B = 0.75
_RRF_K = 60.0
_TOKEN = re.compile(r"[a-z0-9]+(?:[._+#/-][a-z0-9]+)*")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "use", "with",
}


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def terms(value: str) -> list[str]:
    """Return stable code-friendly terms for internal lexical indexing."""

    result: list[str] = []
    for token in _TOKEN.findall(_plain(value)):
        if token in _STOPWORDS:
            continue
        result.append(token)
        # Preserve the exact code token while making dotted/slashed identifiers
        # findable by any meaningful component.
        if any(separator in token for separator in (".", "/", "_", "-")):
            result.extend(
                part
                for part in re.split(r"[./_-]+", token)
                if part and part not in _STOPWORDS and part != token
            )
    return result


def _search_text(entry: dict[str, Any]) -> str:
    return "\n".join(
        str(value)
        for value in (
            entry.get("title", ""),
            entry.get("description", ""),
            entry.get("source", ""),
            entry.get("relative_path", ""),
            " ".join(str(value) for value in entry.get("categories", [])),
            " ".join(str(value) for value in entry.get("tags", [])),
            entry.get("descriptor", ""),
        )
        if value
    )


def _fuzzy_score(query: str, candidate: str) -> float:
    """Score an ordered subsequence with exact/word-boundary bonuses."""

    needle = " ".join(_plain(query).split())
    haystack = " ".join(_plain(candidate).split())
    if not needle or not haystack:
        return 0.0
    if needle == haystack:
        return 10.0
    position = haystack.find(needle)
    if position >= 0:
        boundary = position == 0 or not haystack[position - 1].isalnum()
        return 6.0 + (1.0 if boundary else 0.0) + len(needle) / max(len(haystack), 1)

    cursor = -1
    gaps = 0
    boundaries = 0
    for character in needle:
        if character.isspace():
            continue
        next_position = haystack.find(character, cursor + 1)
        if next_position < 0:
            return 0.0
        if cursor >= 0:
            gaps += max(0, next_position - cursor - 1)
        if next_position == 0 or not haystack[next_position - 1].isalnum():
            boundaries += 1
        cursor = next_position
    compactness = len(needle.replace(" ", "")) / max(cursor + 1, 1)
    return compactness + boundaries * 0.08 + 1.0 / (1.0 + gaps)


@dataclass(frozen=True)
class _Document:
    item_id: str
    frequencies: Counter[str]
    length: int
    fuzzy_text: str


class IntegratedGraphIndex:
    """In-process search and relationship index for the skill catalog."""

    def __init__(self, entries: Iterable[dict[str, Any]]):
        self.entries = {
            str(entry["item_id"]): dict(entry)
            for entry in entries
            if isinstance(entry, dict) and entry.get("item_id")
        }
        self.documents: dict[str, _Document] = {}
        self.document_frequency: Counter[str] = Counter()
        total_length = 0
        for item_id, entry in self.entries.items():
            tokens = terms(_search_text(entry))
            frequencies = Counter(tokens)
            for token in frequencies:
                self.document_frequency[token] += 1
            total_length += len(tokens)
            self.documents[item_id] = _Document(
                item_id=item_id,
                frequencies=frequencies,
                length=len(tokens),
                fuzzy_text=f"{entry.get('graph_key', item_id)} {entry.get('title', '')}",
            )
        self.average_length = total_length / max(len(self.documents), 1)

    def _lexical(self, query: str) -> list[str]:
        query_terms = list(dict.fromkeys(terms(query)))
        if not query_terms:
            return []
        count = len(self.documents)
        scored: list[tuple[str, float]] = []
        for item_id, document in self.documents.items():
            score = 0.0
            length_ratio = document.length / max(self.average_length, 1.0)
            for token in query_terms:
                frequency = document.frequencies.get(token, 0)
                if not frequency:
                    continue
                seen = self.document_frequency[token]
                inverse = math.log(1.0 + (count - seen + 0.5) / (seen + 0.5))
                denominator = frequency + _K1 * (1.0 - _B + _B * length_ratio)
                score += inverse * (frequency * (_K1 + 1.0)) / denominator
            if score > 0:
                scored.append((item_id, score))
        scored.sort(key=lambda value: (-value[1], value[0]))
        return [item_id for item_id, _score in scored]

    def _fuzzy(self, query: str) -> list[str]:
        scored = [
            (item_id, _fuzzy_score(query, document.fuzzy_text))
            for item_id, document in self.documents.items()
        ]
        scored = [value for value in scored if value[1] > 0]
        scored.sort(key=lambda value: (-value[1], value[0]))
        return [item_id for item_id, _score in scored]

    def rank(self, query: str, *, limit: int) -> list[str]:
        rankings = [self._fuzzy(query), self._lexical(query)]
        scores: defaultdict[str, float] = defaultdict(float)
        for ranking in rankings:
            for rank, item_id in enumerate(ranking):
                scores[item_id] += 1.0 / (_RRF_K + rank + 1.0)
        ordered = sorted(scores, key=lambda item_id: (-scores[item_id], item_id))
        return ordered[: max(1, limit)]

    @staticmethod
    def _relation_score(selected: dict[str, Any], other: dict[str, Any]) -> int:
        categories = set(selected.get("categories") or []) & set(other.get("categories") or [])
        tags = set(selected.get("tags") or []) & set(other.get("tags") or [])
        return len(categories) * 4 + len(tags) + int(selected.get("source") == other.get("source")) * 2

    def context(self, item_id: str, *, max_documents: int = 8) -> dict[str, Any]:
        selected = self.entries[item_id]
        related = [
            (self._relation_score(selected, other), other_id, other)
            for other_id, other in self.entries.items()
            if other_id != item_id
        ]
        related = [value for value in related if value[0] > 0]
        related.sort(key=lambda value: (-value[0], str(value[2].get("title", "")).casefold(), value[1]))
        rows = [selected, *(value[2] for value in related[: max(0, max_documents - 1)])]
        documents = []
        edges = []
        selected_categories = set(selected.get("categories") or [])
        selected_tags = set(selected.get("tags") or [])
        for entry in rows:
            key = str(entry.get("graph_key") or entry["item_id"])
            shared_categories = sorted(selected_categories & set(entry.get("categories") or []))
            shared_tags = sorted(selected_tags & set(entry.get("tags") or []))
            documents.append(
                {
                    "key": key,
                    "item_id": entry["item_id"],
                    "title": entry.get("title", ""),
                    "content": str(entry.get("descriptor") or "")[:4800],
                    "source": entry.get("source", ""),
                    "categories": entry.get("categories", []),
                    "tags": entry.get("tags", []),
                }
            )
            if entry is not selected:
                edges.append(
                    {
                        "from": str(selected.get("graph_key") or selected["item_id"]),
                        "to": key,
                        "shared_categories": shared_categories,
                        "shared_tags": shared_tags,
                        "same_source": selected.get("source") == entry.get("source"),
                    }
                )
        return {"documents": documents, "edges": edges, "total": len(documents)}


def catalog_tree(
    entries: Iterable[dict[str, Any]],
    review: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Return a stable source/category graph for operator interfaces."""

    skills = sorted(
        (dict(entry) for entry in entries),
        key=lambda entry: (str(entry.get("title", "")).casefold(), str(entry.get("item_id", ""))),
    )
    review_rows = sorted(
        (dict(entry) for entry in review),
        key=lambda entry: str(entry.get("item_id", "")),
    )
    review_ids = {str(entry.get("item_id", "")) for entry in review_rows}
    all_rows = [*skills, *review_rows]
    sources: defaultdict[str, list[str]] = defaultdict(list)
    categories: defaultdict[str, list[str]] = defaultdict(list)
    tags: defaultdict[str, list[str]] = defaultdict(list)
    states: defaultdict[str, list[str]] = defaultdict(list)
    for entry in all_rows:
        item_id = str(entry["item_id"])
        sources[str(entry.get("source") or "uncategorized")].append(item_id)
        for category in entry.get("categories") or ["uncategorized"]:
            categories[str(category)].append(item_id)
        for tag in entry.get("tags") or []:
            tags[str(tag)].append(item_id)
        states[str(entry.get("state") or "unknown")].append(item_id)

    facets = {
        "sources": {name: children for name, children in sorted(sources.items())},
        "categories": {name: children for name, children in sorted(categories.items())},
        "tags": {name: children for name, children in sorted(tags.items())},
        "states": {name: children for name, children in sorted(states.items())},
    }
    group_kinds = (
        ("source", facets["sources"]),
        ("category", facets["categories"]),
        ("tag", facets["tags"]),
        ("state", facets["states"]),
    )
    nodes = [
        {
            "id": f"{kind}:{name}",
            "kind": kind,
            "label": name,
            "count": len(children),
        }
        for kind, groups in group_kinds
        for name, children in groups.items()
    ]
    nodes.extend(
        {
            "id": f"skill:{entry['item_id']}",
            "kind": "skill",
            "label": entry.get("title", ""),
            "item_id": entry["item_id"],
            "description": entry.get("description", ""),
            "state": entry.get("state", ""),
            "source": entry.get("source", ""),
            "categories": entry.get("categories", []),
            "tags": entry.get("tags", []),
            "canonical_path": entry.get("canonical_path", ""),
            "package_hash": entry.get("package_hash", ""),
            "duplicate_paths": entry.get("duplicate_paths", []),
            "duplicate_sources": entry.get("duplicate_sources", []),
            "duplicate_item_ids": entry.get("duplicate_item_ids", []),
            "review_required": str(entry["item_id"]) in review_ids,
        }
        for entry in all_rows
    )
    edges = [
        {
            "from": f"{kind}:{name}",
            "to": f"skill:{item_id}",
            "relation": kind,
        }
        for kind, groups in group_kinds
        for name, children in groups.items()
        for item_id in children
    ]
    return {
        "schema_version": "retrieval.catalog-tree.v1",
        "roots": [
            {
                "id": f"source:{name}",
                "kind": "source",
                "label": name,
                "children": children,
            }
            for name, children in sorted(sources.items())
        ] + [
            {
                "id": f"category:{name}",
                "kind": "category",
                "label": name,
                "children": children,
            }
            for name, children in sorted(categories.items())
        ],
        "summary": {
            "skills": len(skills),
            "review_required": len(review_rows),
            "sources": len(sources),
            "categories": len(categories),
            "tags": len(tags),
        },
        "facets": facets,
        "nodes": nodes,
        "edges": edges,
        "skills": skills,
        "review": review_rows,
    }
