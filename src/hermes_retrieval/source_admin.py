from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from typing import Any

from .catalog import IweCatalog
from .config import Settings


_SOURCE_NAME = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _restart_watcher() -> dict[str, Any]:
    check = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "hermes-retrieval-watcher.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if check.returncode:
        return {
            "active": False,
            "restarted": False,
            "instruction": "run ./install-watcher.sh to enable continuous intake",
        }
    restart = subprocess.run(
        ["systemctl", "--user", "try-restart", "hermes-retrieval-watcher.service"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if restart.returncode:
        return {
            "active": True,
            "restarted": False,
            "error": (restart.stderr or restart.stdout).strip(),
        }
    return {"active": True, "restarted": True}


class SourceRegistry:
    """Human-only registration for skill roots outside the standard intake tree."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _validate(self, name: str, path: Path) -> Path:
        if not _SOURCE_NAME.fullmatch(name):
            raise ValueError(
                "source name must use lowercase letters, numbers, hyphens, or underscores"
            )
        candidate = path.expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError(f"skill source is not a directory: {candidate}")
        if not any(candidate.rglob("SKILL.md")):
            raise ValueError(f"skill source contains no SKILL.md: {candidate}")
        for source in self.settings.sources():
            if source.name == name:
                raise ValueError(f"source name is already registered: {name}")
            if source.kind == "skills" and source.path.resolve() == candidate:
                raise ValueError(
                    f"skill source path is already registered as {source.name}: {candidate}"
                )
        return candidate

    def register(
        self,
        name: str,
        path: Path,
        *,
        state: str = "cold",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if state not in {"cold", "archived"}:
            raise ValueError("registered intake sources must be cold or archived")
        candidate = self._validate(name, path)
        catalog = IweCatalog(self.settings, self.settings.sources())
        audit = catalog.audit_path(candidate, name)
        result: dict[str, Any] = {
            "name": name,
            "path": str(candidate),
            "state": state,
            "dry_run": dry_run,
            "registered": False,
            "audit": audit,
        }
        if dry_run:
            return result
        if audit["review_required"]:
            raise ValueError(
                f"{audit['review_required']} skill(s) require categories; add exact-ID "
                "entries to category-overrides.toml, then rerun registration"
            )

        current = self.settings.sources_file.read_text(encoding="utf-8")
        # Parse before preserving and appending the human-readable local file.
        tomllib.loads(current)
        block = (
            "\n[[sources]]\n"
            f"name = {json.dumps(name)}\n"
            "kind = \"skills\"\n"
            f"path = {json.dumps(candidate.as_posix())}\n"
            "enabled = true\n"
            f"state = {json.dumps(state)}\n"
        )
        updated = current.rstrip() + "\n" + block
        tomllib.loads(updated)
        _atomic_text(self.settings.sources_file, updated)

        from .service import RetrievalService

        fresh_settings = Settings.load(self.settings.root)
        sync = RetrievalService(fresh_settings).sync([name], reason="source-register")
        result.update(
            {
                "registered": True,
                "sync": sync,
                "watcher": _restart_watcher(),
            }
        )
        return result
