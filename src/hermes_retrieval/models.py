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
    # ``native`` skills are already advertised by a harness, ``hidden`` skills
    # are installed but deliberately omitted from OMP's prompt metadata,
    # ``cold`` skills live only in Retrieval, and ``archived`` skills remain
    # searchable while dormant. A skill may refine a native source to hidden
    # through its own frontmatter; Chroma and IWE never own this state.
    state: str = "cold"
    # Native provenance matters only when a hidden skill is selected. A skill
    # already installed in its owning harness can be returned verbatim without
    # manufacturing a duplicate there, while the other harness still receives
    # an isolated projection copy.
    harness: str = ""


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
