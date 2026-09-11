from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_retrieval.catalog import RetrievalCatalog
from hermes_retrieval.models import SourceConfig


def _settings(tmp_path: Path, root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        catalog_root=root,
        taxonomy_file=Path(__file__).parents[1] / "taxonomy.toml",
        category_overrides_file=tmp_path / "category-overrides.toml",
    )


def _skill(path: Path, name: str, description: str, body: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / "SKILL.md"
    target.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return target


def test_catalog_builds_walkable_source_and_category_graph(tmp_path: Path) -> None:
    _skill(
        tmp_path / "repo" / "skills" / "packet-hunter",
        "Packet Hunter",
        "Hunt suspicious network packets.",
        "Inspect traffic and DNS evidence.",
    )
    root = tmp_path / "catalog"
    catalog = RetrievalCatalog(
        _settings(tmp_path, root),
        [SourceConfig("security", "skills", tmp_path / "repo", state="cold")],
    )

    report = catalog.sync()
    tree = catalog.browse()

    assert report["entries"] == 1
    entry = catalog.entry("security:skills/packet-hunter")
    assert "network-security" in entry["categories"]
    assert len(entry["package_hash"]) == 64
    assert (root / entry["card"]).is_file()
    assert tree["schema_version"] == "retrieval.catalog-tree.v1"
    assert any(root["id"] == "source:security" for root in tree["roots"])


def test_find_uses_integrated_fuzzy_and_bm25_ranking(tmp_path: Path) -> None:
    _skill(
        tmp_path / "cold" / "packets",
        "Packet Analysis",
        "Inspect network packet captures and DNS traffic.",
    )
    _skill(
        tmp_path / "cold" / "slides",
        "Slide Builder",
        "Create presentation decks.",
    )
    catalog = RetrievalCatalog(
        _settings(tmp_path, tmp_path / "catalog"),
        [SourceConfig("cold", "skills", tmp_path / "cold", state="cold")],
    )
    catalog.sync()

    result = catalog.find("network packet", limit=2)

    assert result[0]["item_id"] == "cold:packets"
    assert result[0]["graph_rank"] == 1


def test_identical_packages_merge_provenance_by_content(tmp_path: Path) -> None:
    body = "Inspect network packets and security evidence."
    first = _skill(tmp_path / "first" / "packet", "Packet", body)
    second = _skill(tmp_path / "second" / "packet-copy", "Packet", body)
    (first.parent / "guide.md").write_text("same resource\n", encoding="utf-8")
    (second.parent / "guide.md").write_text("same resource\n", encoding="utf-8")
    catalog = RetrievalCatalog(
        _settings(tmp_path, tmp_path / "catalog"),
        [
            SourceConfig("first", "skills", tmp_path / "first", state="cold"),
            SourceConfig("second", "skills", tmp_path / "second", state="cold"),
        ],
    )

    report = catalog.sync()
    entry = next(iter(catalog.entries().values()))

    assert report["entries"] == 1
    assert report["duplicates_excluded"] == 1
    assert entry["duplicate_sources"] == ["first", "second"]
    assert sorted(entry["duplicate_item_ids"]) == ["first:packet", "second:packet-copy"]


def test_same_title_with_different_content_remains_addressable(tmp_path: Path) -> None:
    _skill(tmp_path / "first" / "packet", "Packet", "Inspect DNS packets.")
    _skill(tmp_path / "second" / "packet", "Packet", "Design presentation slides.")
    catalog = RetrievalCatalog(
        _settings(tmp_path, tmp_path / "catalog"),
        [
            SourceConfig("first", "skills", tmp_path / "first", state="cold"),
            SourceConfig("second", "skills", tmp_path / "second", state="cold"),
        ],
    )

    report = catalog.sync()

    assert report["entries"] == 2
    assert report["duplicates_excluded"] == 0


def test_hidden_native_skill_is_searchable_for_other_harness(tmp_path: Path) -> None:
    skill = _skill(
        tmp_path / "native" / "packet-hidden",
        "Packet Hidden",
        "Audit network packets.",
    )
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("---\n#", "hide: true\n---\n#"),
        encoding="utf-8",
    )
    catalog = RetrievalCatalog(
        _settings(tmp_path, tmp_path / "catalog"),
        [
            SourceConfig(
                "omp", "skills", tmp_path / "native", state="native", harness="omp"
            )
        ],
    )

    report = catalog.sync()
    entry = catalog.entry("omp:packet-hidden")

    assert report["hidden"] == 1
    assert entry["state"] == "hidden"
    assert entry["hidden_harnesses"] == ["omp"]


def test_manifest_version_rejects_stale_external_index(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    (root / ".retrieval-catalog.json").write_text(
        json.dumps({"version": 4, "entries": {"stale": {}}}), encoding="utf-8"
    )
    catalog = RetrievalCatalog(_settings(tmp_path, root), [])

    assert catalog.manifest()["entries"] == {}
