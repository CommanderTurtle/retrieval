"""Session-bound skill consolidation and clean harness baselines."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
import uuid

from filelock import FileLock
import yaml

from .catalog import RetrievalCatalog, bundle_digest
from .config import Settings
from .projection import SkillProjection


_MARKER = ".retrieval-intake.json"
_IGNORED = {".git", "__pycache__", "node_modules", ".venv"}
_ROUTER_PATHS = {
    "hermes": (
        Path("research/retrieve-knowledge"),
        Path("web/localflame"),
        Path("research/librarian"),
    ),
    "omp": (
        Path("retrieve-knowledge"),
        Path("localflame"),
        Path("librarian"),
    ),
}


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return clean[:72] or "skill"


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


def _skill_packages(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    packages: set[Path] = set()
    for skill in root.rglob("SKILL.md"):
        try:
            relative = skill.relative_to(root)
        except ValueError:
            continue
        if any(part in _IGNORED for part in relative.parts):
            continue
        if skill.is_file() and not skill.is_symlink():
            packages.add(skill.parent)
    return sorted(packages, key=lambda path: (len(path.parts), path.as_posix()))


def _contains_links(root: Path) -> bool:
    return any(path.is_symlink() for path in root.rglob("*"))


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path
    stop = stop.resolve()
    while current != stop:
        try:
            current.resolve().relative_to(stop)
            current.rmdir()
        except (OSError, ValueError, yaml.YAMLError):
            break
        current = current.parent


class BaselineManager:
    """Move non-stock skills to Retrieval, index them, then clear host copies."""

    def __init__(
        self,
        settings: Settings,
        harness: str,
        *,
        harness_home: Path | None = None,
    ):
        self.settings = settings
        self.harness = harness.strip().casefold()
        if self.harness not in {"hermes", "omp"}:
            raise ValueError(f"unsupported retrieval harness: {harness}")
        default_home = (
            settings.hermes_config.parent
            if self.harness == "hermes"
            else settings.omp_config.parent
        )
        self.home = (harness_home or default_home).expanduser().resolve()
        self.skills_root = self.home / "skills"
        self.stock_root = (
            settings.hermes_config.parent / "hermes-agent" / "skills"
            if self.harness == "hermes"
            else None
        )
        self.routers = tuple(
            self.skills_root / relative
            for relative in _ROUTER_PATHS[self.harness]
        )
        self.intake = settings.skill_intake_root

    def _consolidate(self, package: Path, origin: str) -> dict[str, Any]:
        skill = package / "SKILL.md"
        digest = bundle_digest(skill)
        destination = self.intake / f"{_slug(package.name)}-{digest[:12]}"
        linked = _contains_links(package)
        created = False
        if not destination.exists():
            self.intake.mkdir(parents=True, exist_ok=True)
            stage = self.intake / f".{destination.name}.stage-{uuid.uuid4().hex}"
            try:
                shutil.copytree(package, stage, symlinks=True)
                if bundle_digest(stage / "SKILL.md") != digest:
                    raise RuntimeError(f"skill changed while being consolidated: {package}")
                stage.rename(destination)
                created = True
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        elif not (destination / "SKILL.md").is_file():
            raise RuntimeError(f"intake collision is not a skill package: {destination}")
        elif bundle_digest(destination / "SKILL.md") != digest:
            raise RuntimeError(f"intake hash collision: {destination}")

        marker_path = destination / _MARKER
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        origins = sorted(
            {
                str(value)
                for value in [*(marker.get("origins") or []), origin]
                if str(value)
            }
        )
        marker = {
            "schema_version": 1,
            "package_hash": digest,
            "origins": origins,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_text(
            marker_path,
            json.dumps(marker, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
        return {
            "source": str(package),
            "destination": str(destination),
            "package_hash": digest,
            "created": created,
            "contains_symlinks": linked,
        }

    def _external_roots(self) -> list[Path]:
        try:
            config_path = self.home / ("config.yaml" if self.harness == "hermes" else "config.yml")
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, yaml.YAMLError):
            return []
        skills = payload.get("skills") if isinstance(payload, dict) else None
        if not isinstance(skills, dict):
            return []
        key = "external_dirs" if self.harness == "hermes" else "customDirectories"
        values = skills.get(key)
        if not isinstance(values, list):
            return []
        projection = self.settings.projection_lane_root(self.harness).resolve()
        roots: list[Path] = []
        for value in values:
            try:
                path = Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()
            except (OSError, ValueError):
                continue
            if path != projection and path.is_dir():
                roots.append(path)
        return roots

    def _restore_stock(self, package: Path, stock: Path) -> None:
        backup = package.parent / f".{package.name}.retrieval-old-{uuid.uuid4().hex}"
        package.rename(backup)
        try:
            shutil.copytree(stock, package, symlinks=True)
        except Exception:
            if package.exists():
                shutil.rmtree(package)
            backup.rename(package)
            raise
        shutil.rmtree(backup)

    def reconcile(self) -> dict[str, Any]:
        lock_path = Path(f"{self.settings.sync_lock_path}.baseline")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=self.settings.sync_lock_timeout):
            consolidated: list[dict[str, Any]] = []
            removed: list[str] = []
            restored: list[str] = []
            skipped: list[dict[str, str]] = []
            actions: list[tuple[str, Path, Path | None]] = []

            # External roots remain untouched; their packages are copied into
            # the durable intake before those roots are retired from host config.
            for external in self._external_roots():
                for package in _skill_packages(external):
                    consolidated.append(
                        self._consolidate(package, f"{self.harness}-external:{package}")
                    )

            self.skills_root.mkdir(parents=True, exist_ok=True)
            for package in _skill_packages(self.skills_root):
                if package.resolve() in {
                    router.resolve(strict=False) for router in self.routers
                }:
                    continue
                relative = package.relative_to(self.skills_root)
                stock = self.stock_root / relative if self.stock_root else None
                if stock is not None and (stock / "SKILL.md").is_file():
                    if bundle_digest(package / "SKILL.md") == bundle_digest(stock / "SKILL.md"):
                        continue
                    row = self._consolidate(
                        package, f"{self.harness}-modified-stock:{relative.as_posix()}"
                    )
                    consolidated.append(row)
                    if row["contains_symlinks"]:
                        skipped.append({"path": str(package), "reason": "contains-symlinks"})
                        continue
                    actions.append(("restore", package, stock))
                    continue

                row = self._consolidate(
                    package, f"{self.harness}-installed:{relative.as_posix()}"
                )
                consolidated.append(row)
                if row["contains_symlinks"]:
                    skipped.append({"path": str(package), "reason": "contains-symlinks"})
                    continue
                actions.append(("remove", package, None))

            # First index the durable copies, then clear only manifest-owned
            # projections and refresh the manifest after host cleanup.
            catalog = RetrievalCatalog(self.settings, self.settings.sources())
            indexed_before_clear = catalog.sync()
            # Nested packages are possible in imported skill collections.  A
            # child must be removed or restored before an enclosing package so
            # every precomputed action remains addressable.
            for action, package, stock in sorted(
                actions, key=lambda row: len(row[1].parts), reverse=True
            ):
                if action == "restore":
                    assert stock is not None
                    self._restore_stock(package, stock)
                    restored.append(str(package))
                else:
                    shutil.rmtree(package)
                    removed.append(str(package))
                    _remove_empty_parents(package.parent, self.skills_root)
            projections = SkillProjection(self.settings, self.harness).clear()
            indexed_after_clear = catalog.sync()
            return {
                "schema_version": "retrieval.session-close.v1",
                "harness": self.harness,
                "home": str(self.home),
                "consolidated": consolidated,
                "removed": removed,
                "restored_stock": restored,
                "preserved_routers": [str(path) for path in self.routers if path.is_dir()],
                "skipped": skipped,
                "projections": projections,
                "indexed_before_clear": indexed_before_clear,
                "indexed_after_clear": indexed_after_clear,
            }
