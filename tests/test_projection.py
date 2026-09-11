from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest
import yaml

from hermes_retrieval.projection import (
    SkillProjection,
    _configure_omp_home,
    _retire_projection_lane,
)


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
        omp_command="omp",
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


def test_omp_projection_is_disabled_and_keeps_canonical(
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
    omp = SkillProjection(settings, "omp")

    with pytest.raises(ValueError, match="current-turn-only"):
        omp.project(entry)
    assert skill.is_file()
    assert not settings.projection_lane_root("omp").exists()


def test_omp_integration_removes_only_old_projection_and_marks_inline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    settings.omp_config = tmp_path / "config.yml"
    omp_root = str(settings.projection_lane_root("omp"))
    settings.omp_config.write_text(
        "advisor:\n  enabled: true\nskills:\n  customDirectories:\n"
        f"    - /existing\n    - {omp_root}\n",
        encoding="utf-8",
    )

    def apply_native_config(
        _command: str,
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> None:
        assert environment == {"PI_CODING_AGENT_DIR": str(tmp_path)}
        assert arguments[:3] == ["config", "set", "skills.customDirectories"]
        payload = yaml.safe_load(settings.omp_config.read_text(encoding="utf-8"))
        payload["skills"]["customDirectories"] = json.loads(arguments[3])
        settings.omp_config.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "hermes_retrieval.projection._run_harness_command",
        apply_native_config,
    )

    first = _configure_omp_home(settings, tmp_path, omp_root)
    second = _configure_omp_home(settings, tmp_path, omp_root)

    omp = yaml.safe_load(settings.omp_config.read_text(encoding="utf-8"))
    assert omp["advisor"]["enabled"] is True
    assert omp["skills"]["customDirectories"] == ["/existing"]
    omp_mcp = json.loads(settings.omp_mcp_config.read_text(encoding="utf-8"))
    assert omp_mcp["mcpServers"]["retrieval"]["env"] == {
        "RETRIEVAL_HARNESS": "omp",
        "RETRIEVAL_DELIVERY": "inline",
    }
    assert first["changed"] is True
    assert second["changed"] is False


def test_retire_legacy_omp_projection_removes_only_manifest_owned_data(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    projection = SkillProjection(settings, "omp")
    target = projection.root / "alpha"
    target.mkdir(parents=True)
    (target / ".retrieval-projection.json").write_text(
        json.dumps({"item_id": "repo:alpha", "harness": "omp"}),
        encoding="utf-8",
    )
    projection.manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "harness": "omp",
                "skills": {
                    "repo:alpha": {
                        "item_id": "repo:alpha",
                        "directory": "alpha",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _retire_projection_lane(projection)

    assert result["removed_skills"] == ["repo:alpha"]
    assert result["retained_unowned"] == []
    assert not projection.root.exists()
