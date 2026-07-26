#!/usr/bin/env python3
"""Install Agent Skills commands and personas as native Hermes slash skills."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tomllib


COMMANDS = {
    "build": "build.toml",
    "code-simplify": "code-simplify.toml",
    "review": "review.toml",
    "ship": "ship.toml",
    "spec": "spec.toml",
    "test": "test.toml",
    "webperf": "webperf.toml",
}
PERSONAS = {
    "code-reviewer": "code-reviewer.md",
    "security-auditor": "security-auditor.md",
    "test-engineer": "test-engineer.md",
    "web-performance-auditor": "web-performance-auditor.md",
}
RUNTIME_PREAMBLE = """
## Hermes runtime

This is a native Hermes slash skill generated from the canonical Agent Skills
command. When the command names another skill, resolve it with the `retrieval`
MCP's `find_skills` and `load_skills` tools instead of assuming it is already
in the prompt.

Agent Skills' Claude hooks and custom-agent tool names are not active here.
For fan-out, load the relevant persona skill with `skill_view`, include that
persona text in the child task, and issue one batched `delegate_task` call so
the children run concurrently. Keep the fan-out flat. For a single persona,
use one `delegate_task` child. If delegation is unavailable, execute the same
persona instructions in the current context and state that fallback.

The system's JavaScript compatibility commands terminate in Bun through
Sandwich. Do not install Node.js, npm, or pnpm.
""".strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-skills", type=Path, required=True)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _command_skill(name: str, source: Path) -> str:
    with source.open("rb") as handle:
        command = tomllib.load(handle)
    description = str(command.get("description") or "").strip()
    prompt = str(command.get("prompt") or "").strip()
    if not description or not prompt:
        raise ValueError(f"incomplete Agent Skills command: {source}")
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description)}\n"
        "version: 1.0.0\n"
        "metadata:\n"
        "  hermes:\n"
        "    tags: [workflow, agent-skills, slash-command]\n"
        "    category: software-development\n"
        "---\n\n"
        f"# /{name}\n\n"
        f"{RUNTIME_PREAMBLE}\n\n"
        "## Canonical command\n\n"
        f"{prompt}\n"
    )


def _write(path: Path, content: str, dry_run: bool) -> str:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return "unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return "would update" if dry_run else "updated"


def main() -> None:
    args = _parser().parse_args()
    source_root = args.agent_skills.resolve()
    target_root = (args.hermes_home.expanduser().resolve() / "skills" / "workflows")
    if not (source_root / "commands").is_dir() or not (source_root / "agents").is_dir():
        raise SystemExit(f"not an Agent Skills checkout: {source_root}")

    for name, filename in COMMANDS.items():
        source = source_root / "commands" / filename
        target = target_root / name / "SKILL.md"
        print(f"{name}: {_write(target, _command_skill(name, source), args.dry_run)}")

    for name, filename in PERSONAS.items():
        source = source_root / "agents" / filename
        if not source.is_file():
            raise SystemExit(f"missing Agent Skills persona: {source}")
        target = target_root / name / "SKILL.md"
        content = source.read_text(encoding="utf-8")
        print(f"{name}: {_write(target, content, args.dry_run)}")

    built_in_plan = args.hermes_home / "skills" / "software-development" / "plan" / "SKILL.md"
    if not built_in_plan.is_file():
        raise SystemExit("Hermes built-in /plan skill is missing")
    print("plan: native Hermes skill present")


if __name__ == "__main__":
    main()
