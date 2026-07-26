from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceConfig:
    name: str
    kind: str
    path: Path
    enabled: bool = True


@dataclass(frozen=True)
class Document:
    record_id: str
    source_name: str
    kind: str
    title: str
    content: str
    locator: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    record_id: str
    source_name: str
    kind: str
    title: str
    locator: str
    content: str
    metadata: dict[str, Any]
    distance: float
    lane: str

    @property
    def score(self) -> float:
        return max(-1.0, min(1.0, 1.0 - self.distance))

