from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_retrieval.source_admin import SourceRegistry


def test_source_registration_dry_run_is_non_mutating(tmp_path: Path) -> None:
    sources_file = tmp_path / "sources.toml"
    sources_file.write_text("", encoding="utf-8")
    source = tmp_path / "future" / "paper-finder" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: Paper Finder\ndescription: Research papers and citations.\n---\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        root=tmp_path,
        sources_file=sources_file,
        sources=lambda: [],
        catalog_root=tmp_path / "catalog",
        iwe_command="iwe",
        taxonomy_file=Path(__file__).parents[1] / "taxonomy.toml",
        category_overrides_file=tmp_path / "category-overrides.toml",
    )

    report = SourceRegistry(settings).register(
        "future-skills", tmp_path / "future", dry_run=True
    )

    assert report["registered"] is False
    assert report["audit"]["approved"] == 1
    assert sources_file.read_text(encoding="utf-8") == ""
