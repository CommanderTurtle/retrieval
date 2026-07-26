from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_retrieval.service import RetrievalService


def test_find_skills_deduplicates_same_file_across_sources(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "sample" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: sample\n---\n", encoding="utf-8")
    service = object.__new__(RetrievalService)
    service.index = SimpleNamespace(
        search=lambda *_args, **_kwargs: [
            SimpleNamespace(
                metadata={"skill_id": "first:sample", "description": "First"},
                title="sample",
                source_name="first",
                locator=str(skill),
                score=0.9,
                lane="fastembed",
            ),
            SimpleNamespace(
                metadata={"skill_id": "second:sample", "description": "Second"},
                title="sample",
                source_name="second",
                locator=str(skill),
                score=0.8,
                lane="fastembed",
            ),
        ]
    )
    service._selected = lambda **_kwargs: []

    result = service.find_skills("sample", limit=8)

    assert [row["skill_id"] for row in result["matches"]] == ["first:sample"]
