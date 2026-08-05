from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from hermes_retrieval.projection import SkillProjection, integrate_harnesses


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        projection_root=tmp_path / "projected",
        projection_max_files=20,
        projection_max_bytes=1024 * 1024,
        hermes_config=tmp_path / "hermes.yaml",
        omp_config=tmp_path / "omp.yml",
    )


def test_projection_copies_full_package_and_clear_keeps_canonical(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical" / "alpha"
    source.mkdir(parents=True)
    skill = source / "SKILL.md"
    skill.write_text("# Alpha\n", encoding="utf-8")
    (source / "reference.md").write_text("Evidence\n", encoding="utf-8")
    settings = _settings(tmp_path)
    projection = SkillProjection(settings)

    projected = projection.project(
        {
            "item_id": "repo:alpha",
            "title": "Alpha",
            "source": "repo",
            "state": "cold",
            "canonical_path": str(skill),
        }
    )

    target = Path(projected["path"])
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# Alpha\n"
    assert (target / "reference.md").is_file()
    assert projection.list()["skills"][0]["available"] is True
    cleared = projection.clear(["repo:alpha"])
    assert cleared["remaining"] == 0
    assert skill.is_file()
    assert not target.exists()


def test_integrate_adds_one_shared_directory_without_losing_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    settings.hermes_config.write_text(
        "model: local\nskills:\n  external_dirs: []\n", encoding="utf-8"
    )
    settings.omp_config.write_text(
        "advisor:\n  enabled: true\nskills:\n  customDirectories:\n    - /existing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_retrieval.projection._prepare_scout_profile",
        lambda *_args: {"profile": "test", "mcp_servers": 0},
    )

    first = integrate_harnesses(settings)
    second = integrate_harnesses(settings)

    hermes = yaml.safe_load(settings.hermes_config.read_text(encoding="utf-8"))
    omp = yaml.safe_load(settings.omp_config.read_text(encoding="utf-8"))
    root = str(settings.projection_root)
    assert hermes["model"] == "local"
    assert hermes["skills"]["external_dirs"] == [root]
    assert omp["advisor"]["enabled"] is True
    assert omp["skills"]["customDirectories"] == ["/existing", root]
    assert first["restart_required"] is False
    assert second["hermes"]["changed"] is False
    assert second["omp"]["changed"] is False
