from pathlib import Path

from hermes_retrieval.chunking import chunk_text, frontmatter, stable_id
from hermes_retrieval.models import SourceConfig
from hermes_retrieval.sources import iter_skills


def test_chunking_is_stable_and_bounded():
    text = "# Title\n\n" + ("sentence. " * 1000)
    first = list(chunk_text(text, max_chars=500, overlap=40))
    second = list(chunk_text(text, max_chars=500, overlap=40))
    assert first == second
    assert all(0 < len(piece) <= 500 for _, piece in first)


def test_frontmatter_reads_scalar_fields():
    assert frontmatter("---\nname: useful\ndescription: does a thing\n---\nBody") == {
        "name": "useful",
        "description": "does a thing",
    }


def test_skill_source_uses_explicit_root(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Finds alpha.\n---\n# Alpha\n\nInstructions.",
        encoding="utf-8",
    )
    source = SourceConfig("repo", "skills", tmp_path / "skills")
    docs = list(iter_skills(source))
    assert docs
    assert docs[0].metadata["skill_id"] == "repo:alpha"
    assert docs[0].metadata["description"] == "Finds alpha."
    assert docs[0].record_id == stable_id("skills", "repo", "alpha/SKILL.md", 0)

