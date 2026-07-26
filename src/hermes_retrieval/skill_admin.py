from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from .chunking import frontmatter, markdown_title
from .models import SourceConfig
from .sources import _iter_skill_paths, _skill_id

_ARCHIVE_DIRECTORY = ".retrieval-archive"


class SkillAdminError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _skill_sources(sources: list[SourceConfig]) -> list[SourceConfig]:
    return [
        source
        for source in sources
        if source.enabled and source.kind == "skills" and source.path.is_dir()
    ]


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


def _safe_join(root: Path, relative: str) -> Path:
    candidate = (root / relative).absolute()
    _relative(candidate, root)
    return candidate


def _source_archive_root(archive_root: Path, source: SourceConfig) -> Path:
    digest = hashlib.sha256(str(source.path).encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in source.name
    ).strip("-_") or "skills"
    return archive_root / f"{safe_name}-{digest}"


def _manifest_path(archive_root: Path, source: SourceConfig) -> Path:
    return _source_archive_root(archive_root, source) / "manifest.json"


def _read_manifest(archive_root: Path, source: SourceConfig) -> dict[str, Any]:
    path = _manifest_path(archive_root, source)
    source_archive = path.parent
    if archive_root.is_symlink() or source_archive.is_symlink() or path.is_symlink():
        raise SkillAdminError(f"refusing symlinked archive path: {source_archive}")
    if not path.exists():
        return {"version": 1, "items": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillAdminError(f"invalid skill archive manifest: {path}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("items"), dict):
        raise SkillAdminError(f"unsupported skill archive manifest: {path}")
    return payload


def _write_manifest(
    archive_root: Path,
    source: SourceConfig,
    payload: dict[str, Any],
) -> None:
    path = _manifest_path(archive_root, source)
    source_archive = path.parent
    if archive_root.is_symlink() or source_archive.is_symlink() or path.is_symlink():
        raise SkillAdminError(f"refusing symlinked archive path: {source_archive}")
    archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlinks(source_archive, archive_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".manifest-",
        suffix=".json",
        dir=source_archive,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _active_entry(source: SourceConfig, logical: Path) -> dict[str, Any]:
    canonical = logical.resolve()
    text = canonical.read_text(encoding="utf-8", errors="replace")
    metadata = frontmatter(text)
    file_stat = canonical.stat()
    return {
        "skill_id": _skill_id(source, logical),
        "name": metadata.get("name")
        or markdown_title(text, logical.parent.name or source.name),
        "description": metadata.get("description", ""),
        "source": source.name,
        "state": "active",
        "path": str(logical.absolute()),
        "canonical_path": str(canonical),
        "relative_path": _relative(logical, source.path).as_posix(),
        "mtime_ns": file_stat.st_mtime_ns,
        "modified_at": datetime.fromtimestamp(
            file_stat.st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        "bytes": file_stat.st_size,
        "symlinked": any(
            (source.path / Path(*_relative(logical, source.path).parts[:index])).is_symlink()
            for index in range(1, len(_relative(logical, source.path).parts) + 1)
        ),
    }


def _archived_skill_path(
    archive_root: Path,
    source: SourceConfig,
    record: dict[str, Any],
) -> Path:
    source_archive = _source_archive_root(archive_root, source)
    payload = _safe_join(source_archive, str(record["archive_relative"]))
    if record.get("payload_type") == "directory":
        return payload / "SKILL.md"
    return payload


def _move_payload(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if source.is_dir():
        shutil.copytree(source, target, copy_function=shutil.copy2)
        shutil.rmtree(source)
    else:
        shutil.copy2(source, target)
        source.unlink()


class SkillAdmin:
    def __init__(
        self,
        sources: list[SourceConfig],
        archive_root: Path,
    ) -> None:
        self.sources = _skill_sources(sources)
        self.archive_root = archive_root.absolute()
        if self.archive_root.is_symlink():
            raise SkillAdminError(
                f"refusing symlinked skill archive root: {self.archive_root}"
            )
        for source in self.sources:
            try:
                self.archive_root.relative_to(source.path.absolute())
            except ValueError:
                try:
                    source.path.absolute().relative_to(self.archive_root)
                except ValueError:
                    continue
            raise SkillAdminError(
                "skill archive and configured skill source must not overlap: "
                f"{source.path}"
            )

    def _active(self) -> dict[str, tuple[SourceConfig, dict[str, Any]]]:
        result: dict[str, tuple[SourceConfig, dict[str, Any]]] = {}
        for source in self.sources:
            for logical in _iter_skill_paths(source.path):
                try:
                    entry = _active_entry(source, logical)
                except (FileNotFoundError, OSError):
                    continue
                skill_id = str(entry["skill_id"])
                if skill_id in result:
                    raise SkillAdminError(f"duplicate exact skill ID: {skill_id}")
                result[skill_id] = (source, entry)
        return result

    def _archived(self) -> dict[str, tuple[SourceConfig, dict[str, Any]]]:
        result: dict[str, tuple[SourceConfig, dict[str, Any]]] = {}
        for source in self.sources:
            for skill_id, record in _read_manifest(
                self.archive_root,
                source,
            )["items"].items():
                if skill_id in result:
                    raise SkillAdminError(f"duplicate archived skill ID: {skill_id}")
                path = _archived_skill_path(self.archive_root, source, record)
                entry = {
                    **record,
                    "skill_id": skill_id,
                    "source": source.name,
                    "state": "archived",
                    "path": str(path),
                    "canonical_path": str(path),
                    "symlinked": False,
                }
                result[skill_id] = (source, entry)
        return result

    def list(self) -> list[dict[str, Any]]:
        active = self._active()
        archived = self._archived()
        overlap = set(active) & set(archived)
        if overlap:
            raise SkillAdminError(
                "skill IDs exist in both active and archived state: "
                + ", ".join(sorted(overlap))
            )
        entries = [
            entry
            for _source, entry in [
                *active.values(),
                *archived.values(),
            ]
        ]
        return sorted(
            entries,
            key=lambda item: (
                -int(item.get("mtime_ns") or 0),
                str(item.get("name") or "").casefold(),
                str(item["skill_id"]),
            ),
        )

    def inspect(self, skill_id: str) -> dict[str, Any]:
        if skill_id in (active := self._active()):
            _source, entry = active[skill_id]
        elif skill_id in (archived := self._archived()):
            _source, entry = archived[skill_id]
        else:
            raise SkillAdminError(f"unknown exact skill ID: {skill_id}")
        path = Path(str(entry["canonical_path"]))
        if not path.is_file():
            raise SkillAdminError(f"skill content is unavailable: {path}")
        return {
            **entry,
            "content": path.read_text(encoding="utf-8", errors="replace"),
        }

    def edit(self, skill_id: str) -> int:
        active = self._active()
        if skill_id not in active:
            if skill_id in self._archived():
                raise SkillAdminError(
                    f"{skill_id} is archived; restore it before editing"
                )
            raise SkillAdminError(f"unknown exact skill ID: {skill_id}")
        source, entry = active[skill_id]
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

    def archive(self, skill_id: str) -> dict[str, Any]:
        active = self._active()
        if skill_id not in active:
            if skill_id in self._archived():
                raise SkillAdminError(f"skill is already archived: {skill_id}")
            raise SkillAdminError(f"unknown exact skill ID: {skill_id}")
        source, entry = active[skill_id]
        skill_path = Path(str(entry["path"]))
        _assert_no_symlinks(skill_path, source.path)
        if skill_path.parent == source.path.absolute():
            payload = skill_path
            payload_type = "file"
        else:
            payload = skill_path.parent
            nested = [
                path
                for path in payload.rglob("SKILL.md")
                if path.absolute() != skill_path.absolute()
                and _ARCHIVE_DIRECTORY not in path.parts
            ]
            if nested:
                raise SkillAdminError(
                    "refusing to archive a directory that contains other skills: "
                    + ", ".join(str(path) for path in nested[:5])
                )
            payload_type = "directory"
        _assert_no_symlinks(payload, source.path)
        original_relative = _relative(payload, source.path).as_posix()
        archive_root = _source_archive_root(self.archive_root, source)
        target = _safe_join(
            archive_root,
            f"payloads/{original_relative}",
        )
        if target.exists() or target.is_symlink():
            raise SkillAdminError(f"archive target already exists: {target}")
        self.archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _assert_no_symlinks(archive_root.parent, self.archive_root)
        _assert_no_symlinks(target.parent, self.archive_root)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _assert_no_symlinks(target.parent, self.archive_root)
        payload_stat = payload.stat()
        record = {
            "name": entry["name"],
            "description": entry["description"],
            "relative_path": entry["relative_path"],
            "original_relative": original_relative,
            "archive_relative": _relative(target, archive_root).as_posix(),
            "payload_type": payload_type,
            "archived_at": _utc_now(),
            "mtime_ns": int(entry["mtime_ns"]),
            "modified_at": entry["modified_at"],
            "mode": stat.S_IMODE(payload_stat.st_mode),
            "bytes": int(entry["bytes"]),
        }
        manifest = _read_manifest(self.archive_root, source)
        manifest["items"][skill_id] = record
        _move_payload(payload, target)
        try:
            _write_manifest(self.archive_root, source, manifest)
        except Exception:
            payload.parent.mkdir(parents=True, exist_ok=True)
            _move_payload(target, payload)
            raise
        return {"skill_id": skill_id, "state": "archived", **record}

    def restore(self, skill_id: str) -> dict[str, Any]:
        archived = self._archived()
        if skill_id not in archived:
            if skill_id in self._active():
                raise SkillAdminError(f"skill is already active: {skill_id}")
            raise SkillAdminError(f"unknown exact archived skill ID: {skill_id}")
        source, record = archived[skill_id]
        manifest = _read_manifest(self.archive_root, source)
        saved = manifest["items"][skill_id]
        archive_root = _source_archive_root(self.archive_root, source)
        payload = _safe_join(archive_root, str(saved["archive_relative"]))
        _assert_no_symlinks(payload, self.archive_root)
        target = _safe_join(source.path, str(saved["original_relative"]))
        _assert_no_symlinks(target.parent, source.path)
        if not payload.exists():
            raise SkillAdminError(f"archived payload is missing: {payload}")
        if target.exists() or target.is_symlink():
            raise SkillAdminError(f"restore target already exists: {target}")
        _assert_no_symlinks(target.parent, source.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlinks(target.parent, source.path)
        _move_payload(payload, target)
        del manifest["items"][skill_id]
        try:
            _write_manifest(self.archive_root, source, manifest)
        except Exception:
            payload.parent.mkdir(parents=True, exist_ok=True)
            _move_payload(target, payload)
            raise
        return {
            "skill_id": skill_id,
            "state": "active",
            "path": str(
                target / "SKILL.md"
                if saved.get("payload_type") == "directory"
                else target
            ),
            "restored_at": _utc_now(),
        }
