from pathlib import Path
import runpy


def test_agent_workflow_installer_creates_native_slash_skills(
    tmp_path: Path,
    monkeypatch,
):
    repository = tmp_path / "agent-skills"
    commands = repository / "commands"
    agents = repository / "agents"
    commands.mkdir(parents=True)
    agents.mkdir()
    for name in (
        "build",
        "code-simplify",
        "review",
        "ship",
        "spec",
        "test",
        "webperf",
    ):
        (commands / f"{name}.toml").write_text(
            f'description = "{name} workflow"\nprompt = "Run {name} carefully."\n',
            encoding="utf-8",
        )
    for name in (
        "code-reviewer",
        "security-auditor",
        "test-engineer",
        "web-performance-auditor",
    ):
        (agents / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {name} persona\n---\n# {name}\n",
            encoding="utf-8",
        )

    hermes_home = tmp_path / ".hermes"
    plan = hermes_home / "skills" / "software-development" / "plan" / "SKILL.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nname: plan\n---\n# Plan\n", encoding="utf-8")

    script = Path(__file__).parents[1] / "scripts" / "install-agent-workflows.py"
    monkeypatch.setattr(
        "sys.argv",
        [
            str(script),
            "--agent-skills",
            str(repository),
            "--hermes-home",
            str(hermes_home),
        ],
    )
    runpy.run_path(str(script), run_name="__main__")

    ship = hermes_home / "skills" / "workflows" / "ship" / "SKILL.md"
    assert "name: ship" in ship.read_text(encoding="utf-8")
    assert "delegate_task" in ship.read_text(encoding="utf-8")
    reviewer = (
        hermes_home / "skills" / "workflows" / "code-reviewer" / "SKILL.md"
    )
    assert "name: code-reviewer" in reviewer.read_text(encoding="utf-8")
