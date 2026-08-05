from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_retrieval.catalog import IweCatalog
from hermes_retrieval.models import SourceConfig


def _settings(tmp_path: Path, root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        catalog_root=root,
        iwe_command="iwe",
        taxonomy_file=Path(__file__).parents[1] / "taxonomy.toml",
        category_overrides_file=tmp_path / "category-overrides.toml",
    )


def test_iwe_catalog_builds_walkable_source_and_category_hubs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill = tmp_path / "repo" / "skills" / "packet-hunter" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: Packet Hunter\n"
        "description: Hunt suspicious network packets.\n"
        "tags: [network, detection]\n"
        "---\n"
        "# Packet Hunter\n\n"
        "Inspect traffic and DNS evidence.\n",
        encoding="utf-8",
    )
    root = tmp_path / "catalog"
    settings = _settings(tmp_path, root)
    source = SourceConfig("security", "skills", tmp_path / "repo", state="cold")
    catalog = IweCatalog(settings, [source])
    monkeypatch.setattr(catalog, "_iwe", lambda: "iwe")
    monkeypatch.setattr(
        "hermes_retrieval.catalog.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    report = catalog.sync()

    assert report["entries"] == 1
    manifest = json.loads(
        (root / ".retrieval-catalog.json").read_text(encoding="utf-8")
    )
    entry = manifest["entries"]["security:skills/packet-hunter"]
    assert entry["state"] == "cold"
    assert "network-security" in entry["categories"]
    card = root / entry["card"]
    assert card.is_file()
    source_hub = (root / "sources" / "security.md").read_text(encoding="utf-8")
    assert f"](../{entry['card']})" in source_hub


def test_iwe_find_filters_native_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    payload = {
        "version": 2,
        "entries": {
            "cold:a": {
                "item_id": "cold:a",
                "iwe_key": "skills/cold/a",
                "state": "cold",
            },
            "native:b": {
                "item_id": "native:b",
                "iwe_key": "skills/native/b",
                "state": "native",
            },
        },
        "owned_files": [],
    }
    (root / ".retrieval-catalog.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    settings = _settings(tmp_path, root)
    catalog = IweCatalog(settings, [])
    monkeypatch.setattr(catalog, "_iwe", lambda: "iwe")
    monkeypatch.setattr(
        "hermes_retrieval.catalog.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {"key": "skills/native/b"},
                    {"key": "skills/cold/a"},
                ]
            ),
            stderr="",
        ),
    )

    assert [row["item_id"] for row in catalog.find("a")] == ["cold:a"]


def test_native_name_suppresses_duplicate_cold_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    native = tmp_path / "native" / "humanizer" / "SKILL.md"
    cold = tmp_path / "cold" / "humanizer" / "SKILL.md"
    native.parent.mkdir(parents=True)
    cold.parent.mkdir(parents=True)
    body = "---\nname: Humanizer\ndescription: Improve prose.\n---\n# Humanizer\n"
    native.write_text(body, encoding="utf-8")
    cold.write_text(body, encoding="utf-8")
    root = tmp_path / "catalog"
    settings = _settings(tmp_path, root)
    catalog = IweCatalog(
        settings,
        [
            SourceConfig("cold", "skills", tmp_path / "cold", state="cold"),
            SourceConfig("native", "skills", tmp_path / "native", state="native"),
        ],
    )
    monkeypatch.setattr(catalog, "_iwe", lambda: "iwe")
    monkeypatch.setattr(
        "hermes_retrieval.catalog.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    report = catalog.sync()

    assert report["entries"] == 0
    assert report["native_excluded"] == 1
    assert catalog.entries() == {}


def test_uncategorized_skill_requires_review_and_is_not_graphed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill = tmp_path / "cold" / "peculiar" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: Peculiar\ndescription: Zorb the quux.\n---\n# Peculiar\n",
        encoding="utf-8",
    )
    root = tmp_path / "catalog"
    catalog = IweCatalog(
        _settings(tmp_path, root),
        [SourceConfig("cold", "skills", tmp_path / "cold", state="cold")],
    )
    monkeypatch.setattr(catalog, "_iwe", lambda: "iwe")
    monkeypatch.setattr(
        "hermes_retrieval.catalog.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    report = catalog.sync()

    assert report["entries"] == 0
    assert report["review_required"] == 1
    assert catalog.audit()["review"][0]["skill_id"] == "cold:peculiar"
    assert not (root / "skills" / "cold").exists()


def test_local_override_promotes_reviewed_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill = tmp_path / "cold" / "peculiar" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: Peculiar\ndescription: Zorb the quux.\n---\n# Peculiar\n",
        encoding="utf-8",
    )
    (tmp_path / "category-overrides.toml").write_text(
        '[skills]\n"cold:peculiar" = ["research"]\n',
        encoding="utf-8",
    )
    root = tmp_path / "catalog"
    catalog = IweCatalog(
        _settings(tmp_path, root),
        [SourceConfig("cold", "skills", tmp_path / "cold", state="cold")],
    )
    monkeypatch.setattr(catalog, "_iwe", lambda: "iwe")
    monkeypatch.setattr(
        "hermes_retrieval.catalog.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    report = catalog.sync()

    assert report["entries"] == 1
    assert report["review_required"] == 0
    assert catalog.entry("cold:peculiar")["categories"] == ["research"]
