from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

from .chunking import frontmatter, markdown_title
from .models import SourceConfig
from .sources import _iter_skill_paths, _skill_id, _skill_state


class SkillAdminError(RuntimeError):
    pass


def _relative(path: Path, root: Path) -> Path:
    try:
        return path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SkillAdminError(f"path escapes configured source root: {path}") from exc


def _assert_no_symlinks(path: Path, root: Path) -> None:
    relative = _relative(path, root)
    cursor = root.absolute()
    if cursor.is_symlink():
        raise SkillAdminError(f"configured source root is a symlink: {root}")
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise SkillAdminError(f"refusing symlinked skill path: {cursor}")


class SkillAdmin:
    """Human-only exact-ID inspection for configured canonical skill trees.

    Archive lifecycle deliberately belongs to the harness. Hermes users use
    ``hermes curator archive|restore``; Retrieval observes the canonical
    ``~/.hermes/skills/.archive`` source read-only.
    """

    def __init__(self, sources: list[SourceConfig]) -> None:
        self.sources = [
            source
            for source in sources
            if source.enabled and source.kind == "skills" and source.path.is_dir()
        ]

    def _entries(self) -> dict[str, tuple[SourceConfig, dict[str, Any]]]:
        result: dict[str, tuple[SourceConfig, dict[str, Any]]] = {}
        for source in self.sources:
            for logical in _iter_skill_paths(source.path):
                canonical = logical.resolve()
                if not canonical.is_file():
                    continue
                text = canonical.read_text(encoding="utf-8", errors="replace")
                metadata = frontmatter(text)
                stat = canonical.stat()
                skill_id = _skill_id(source, logical.absolute())
                entry = {
                    "skill_id": skill_id,
                    "name": metadata.get("name")
                    or markdown_title(text, logical.parent.name or source.name),
                    "description": metadata.get("description", ""),
                    "source": source.name,
                    "state": _skill_state(source, text),
                    "path": str(logical.absolute()),
                    "canonical_path": str(canonical),
                    "relative_path": _relative(logical, source.path).as_posix(),
                    "mtime_ns": stat.st_mtime_ns,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                    "bytes": stat.st_size,
                    "symlinked": logical.is_symlink() or logical.parent.is_symlink(),
                }
                if skill_id in result:
                    raise SkillAdminError(f"duplicate exact skill ID: {skill_id}")
                result[skill_id] = (source, entry)
        return result

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            (entry for _source, entry in self._entries().values()),
            key=lambda item: (
                -int(item.get("mtime_ns") or 0),
                str(item.get("name") or "").casefold(),
                str(item["skill_id"]),
            ),
        )

    def inspect(self, skill_id: str) -> dict[str, Any]:
        entries = self._entries()
        if skill_id not in entries:
            raise SkillAdminError(f"unknown exact skill ID: {skill_id}")
        _source, entry = entries[skill_id]
        path = Path(str(entry["canonical_path"]))
        return {
            **entry,
            "content": path.read_text(encoding="utf-8", errors="replace"),
        }

    def edit(self, skill_id: str) -> int:
        entries = self._entries()
        if skill_id not in entries:
            raise SkillAdminError(f"unknown exact skill ID: {skill_id}")
        source, entry = entries[skill_id]
        if source.state in {"native", "archived"}:
            raise SkillAdminError(
                "Retrieval will not edit harness-owned native or archived skills: "
                f"{entry['path']}"
            )
        path = Path(str(entry["path"]))
        _assert_no_symlinks(path, source.path)
        editor = (os.getenv("VISUAL") or os.getenv("EDITOR") or "").strip()
        if not editor:
            raise SkillAdminError(
                f"VISUAL/EDITOR is not set. Edit this exact path: {path}"
            )
        command = shlex.split(editor)
        if not command:
            raise SkillAdminError(
                f"VISUAL/EDITOR is empty. Edit this exact path: {path}"
            )
        return subprocess.run([*command, str(path)], check=False).returncode
