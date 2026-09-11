from pathlib import Path

from hermes_retrieval.projection import _remove_managed_routing_skill


def test_remove_managed_router_accepts_an_older_revision(tmp_path: Path) -> None:
    target = tmp_path / "research" / "retrieve-knowledge"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: retrieve-knowledge\ndescription: old revision\n---\n\nOld body.\n",
        encoding="utf-8",
    )
    (target / "extra.txt").write_text("managed bundle file\n", encoding="utf-8")

    assert _remove_managed_routing_skill(target) is True
    assert not target.exists()


def test_remove_managed_router_rejects_an_unrelated_skill(tmp_path: Path) -> None:
    target = tmp_path / "research" / "retrieve-knowledge"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: user-skill\ndescription: unrelated\n---\n\nKeep me.\n",
        encoding="utf-8",
    )

    assert _remove_managed_routing_skill(target) is False
    assert target.is_dir()
