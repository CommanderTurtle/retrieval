from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import yaml

from hermes_retrieval.projection import SkillProjection, integrate_harnesses


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path / "retrieval",
        projection_root=tmp_path / "projected",
        projection_lane_root=lambda harness: (
            tmp_path / "projected" / harness / "skills"
        ),
        projection_max_files=20,
        projection_max_bytes=1024 * 1024,
        hermes_config=tmp_path / "hermes.yaml",
        omp_config=tmp_path / "omp.yml",
        omp_mcp_config=tmp_path / "mcp.json",
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
    projection = SkillProjection(settings, "hermes")

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


def test_two_projection_lanes_are_independent_and_keep_canonical(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical" / "alpha"
    source.mkdir(parents=True)
    skill = source / "SKILL.md"
    skill.write_text("# Alpha\n", encoding="utf-8")
    entry = {
        "item_id": "repo:alpha",
        "title": "Alpha",
        "source": "repo",
        "state": "cold",
        "canonical_path": str(skill),
    }
    settings = _settings(tmp_path)
    hermes = SkillProjection(settings, "hermes")
    omp = SkillProjection(settings, "omp")

    hermes_path = Path(hermes.project(entry)["path"])
    omp_path = Path(omp.project(entry)["path"])
    hermes.clear(["repo:alpha"])

    assert not hermes_path.exists()
    assert omp_path.is_dir()
    assert skill.is_file()
    assert [row["item_id"] for row in omp.list()["skills"]] == ["repo:alpha"]


def test_integrate_adds_isolated_directories_without_losing_settings(
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
    hermes_root = str(settings.projection_lane_root("hermes"))
    omp_root = str(settings.projection_lane_root("omp"))
    assert hermes["model"] == "local"
    assert hermes["skills"]["external_dirs"] == [hermes_root]
    assert hermes["mcp_servers"]["retrieval"]["env"] == {
        "RETRIEVAL_HARNESS": "hermes"
    }
    assert omp["advisor"]["enabled"] is True
    assert omp["skills"]["customDirectories"] == ["/existing", omp_root]
    omp_mcp = json.loads(settings.omp_mcp_config.read_text(encoding="utf-8"))
    assert omp_mcp["mcpServers"]["retrieval"]["env"] == {
        "RETRIEVAL_HARNESS": "omp"
    }
    assert first["restart_required"] is True
    assert second["hermes"]["changed"] is False
    assert second["omp"]["changed"] is False
