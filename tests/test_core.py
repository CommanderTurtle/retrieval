from pathlib import Path

import pytest

from hermes_retrieval.chunking import chunk_text, frontmatter, stable_id
from hermes_retrieval.models import SourceConfig
from hermes_retrieval.sources import (
    iter_skills,
    iter_workflows,
    skill_catalog,
    skill_bundle_files,
    workflow_catalog,
)


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


def test_skill_source_follows_linked_skill_directory(tmp_path: Path):
    skill_dir = tmp_path / "repository" / "skills" / "sandwich-runtime"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text(
        "---\nname: sandwich-runtime\ndescription: Uses Bun.\n---\n# Sandwich",
        encoding="utf-8",
    )
    installed = tmp_path / "installed"
    installed.mkdir()
    linked = installed / "sandwich-runtime"
    try:
        linked.symlink_to(skill_dir, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require an unavailable host privilege")
    source = SourceConfig("hermes-skills", "skills", installed)

    docs = list(iter_skills(source))
    assert docs
    assert docs[0].metadata["skill_id"] == "hermes-skills:sandwich-runtime"
    assert docs[0].locator == str(skill.resolve())
    assert skill_catalog([source])["hermes-skills:sandwich-runtime"][1] == skill.resolve()


def test_skill_bundle_follows_relative_docs_and_lists_resources(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text("# Alpha\n\nRead [details](details.md).", encoding="utf-8")
    details = skill_dir / "details.md"
    details.write_text("# Details\n\nExact guidance.", encoding="utf-8")
    (skill_dir / "script.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    source = SourceConfig("repo", "skills", tmp_path / "skills")
    selected, resources = skill_bundle_files(source, skill)
    assert selected == [skill.resolve(), details.resolve()]
    assert {row["relative_path"] for row in resources} == {
        "SKILL.md",
        "details.md",
        "script.sh",
    }


def test_workflows_are_discoverable_but_not_activated(tmp_path: Path):
    agents = tmp_path / "agents"
    commands = tmp_path / "commands"
    hooks = tmp_path / "hooks"
    agents.mkdir()
    commands.mkdir()
    hooks.mkdir()
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews code.\n---\n# Reviewer",
        encoding="utf-8",
    )
    (commands / "ship.toml").write_text(
        'description = "Ships safely."\nprompt = "Review first."\n',
        encoding="utf-8",
    )
    (hooks / "session-start.sh").write_text(
        "#!/bin/sh\n# Injects instructions.\n",
        encoding="utf-8",
    )
    (hooks / "session-start-test.sh").write_text(
        "#!/bin/sh\n# Tests only.\n",
        encoding="utf-8",
    )
    source = SourceConfig("repo", "workflows", tmp_path)
    docs = list(iter_workflows(source))
    assert {doc.metadata["workflow_type"] for doc in docs} == {
        "agent",
        "command",
        "hook",
    }
    catalog = workflow_catalog([source])
    assert set(catalog) == {
        "repo:agents/reviewer.md",
        "repo:commands/ship.toml",
        "repo:hooks/session-start.sh",
    }
