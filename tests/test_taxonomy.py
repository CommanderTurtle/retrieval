from __future__ import annotations

from pathlib import Path

import pytest

from hermes_retrieval.taxonomy import load_category_overrides, load_taxonomy


def test_taxonomy_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "taxonomy.toml"
    path.write_text(
        "version = 1\n"
        "[[categories]]\n"
        'id = "research"\nlabel = "Research"\nkeywords = ["paper"]\n'
        "[[categories]]\n"
        'id = "research"\nlabel = "Again"\nkeywords = ["study"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate Retrieval category"):
        load_taxonomy(path)


def test_override_rejects_unknown_category(tmp_path: Path) -> None:
    taxonomy_path = tmp_path / "taxonomy.toml"
    taxonomy_path.write_text(
        "version = 1\n"
        "[[categories]]\n"
        'id = "research"\nlabel = "Research"\nkeywords = ["paper"]\n',
        encoding="utf-8",
    )
    overrides = tmp_path / "category-overrides.toml"
    overrides.write_text(
        '[skills]\n"dump:item" = ["invented"]\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="unknown Retrieval categories"):
        load_category_overrides(overrides, load_taxonomy(taxonomy_path))
