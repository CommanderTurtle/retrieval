from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any
import uuid

import yaml

from .config import Settings


_MANIFEST_NAME = ".retrieval-projections.json"
_MARKER_NAME = ".retrieval-projection.json"
_IGNORED = {".git", ".venv", "__pycache__", "node_modules"}
_HARNESSES = {"hermes", "omp"}


def _reload_command(harness: str) -> str:
    return "/reload-skills" if harness == "hermes" else "/reload"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:72] or "skill"


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=False)
    resolved.relative_to(root.resolve())
    return resolved


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_owned_tree(root: Path, target: Path) -> None:
    target = _inside(root, target)
    if target == root.resolve():
        raise RuntimeError("refusing to remove the projection root")
    if target.is_dir():
        shutil.rmtree(target)


class SkillProjection:
    """Own one harness's temporary copies without mutating canonical libraries."""

    def __init__(self, settings: Settings, harness: str):
        self.settings = settings
        self.harness = harness.strip().casefold()
        if self.harness not in _HARNESSES:
            raise ValueError(f"unsupported retrieval harness: {harness}")
        self.root = settings.projection_lane_root(self.harness)
        self.manifest_path = self.root / _MANIFEST_NAME

    def _manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 2, "harness": self.harness, "skills": {}}
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 2
            or payload.get("harness") != self.harness
            or not isinstance(payload.get("skills"), dict)
        ):
            raise RuntimeError(
                f"invalid projection manifest: {self.manifest_path}"
            )
        return payload

    def _write_manifest(self, payload: dict[str, Any]) -> None:
        _atomic_text(
            self.manifest_path,
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def _files(self, source_root: Path) -> tuple[list[Path], list[str], int]:
        files: list[Path] = []
        skipped: list[str] = []
        total = 0
        for current, directories, names in os.walk(source_root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED
                and not (current_path / name).is_symlink()
            ]
            for name in sorted(names):
                path = current_path / name
                relative = path.relative_to(source_root).as_posix()
                if name in _IGNORED or path.is_symlink():
                    skipped.append(relative)
                    continue
                resolved = path.resolve()
                resolved.relative_to(source_root.resolve())
                if not resolved.is_file():
                    continue
                total += resolved.stat().st_size
                files.append(resolved)
                if len(files) > self.settings.projection_max_files:
                    raise ValueError(
                        f"skill bundle exceeds {self.settings.projection_max_files} files"
                    )
                if total > self.settings.projection_max_bytes:
                    raise ValueError(
                        "skill bundle exceeds the configured projection byte limit"
                    )
        return files, skipped, total

    def project(self, entry: dict[str, Any]) -> dict[str, Any]:
        item_id = str(entry["item_id"])
        skill_path = Path(str(entry["canonical_path"])).resolve()
        if not skill_path.is_file() or skill_path.name != "SKILL.md":
            raise ValueError(f"canonical SKILL.md is unavailable: {skill_path}")
        source_root = skill_path.parent
        files, skipped, total = self._files(source_root)
        digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:10]
        directory_name = f"{_slug(str(entry['title']))}-{digest}"
        target = _inside(self.root, self.root / directory_name)
        self.root.mkdir(parents=True, exist_ok=True)

        manifest = self._manifest()
        current = manifest["skills"].get(item_id)
        if target.exists() and (
            not isinstance(current, dict)
            or current.get("directory") != directory_name
        ):
            raise RuntimeError(
                f"projection target exists but is not manifest-owned: {target}"
            )

        stage = Path(
            tempfile.mkdtemp(prefix=f".{directory_name}.stage-", dir=self.root)
        )
        backup: Path | None = None
        try:
            for source_file in files:
                relative = source_file.relative_to(source_root)
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination)
            marker = {
                "item_id": item_id,
                "harness": self.harness,
                "source": entry["source"],
                "canonical_path": str(skill_path),
                "projected_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_text(
                stage / _MARKER_NAME,
                json.dumps(marker, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            )
            if target.exists():
                existing_marker = target / _MARKER_NAME
                try:
                    owned = json.loads(existing_marker.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"projection marker is missing or invalid: {target}"
                    ) from exc
                if owned.get("item_id") != item_id:
                    raise RuntimeError(f"projection ownership mismatch: {target}")
                if owned.get("harness") != self.harness:
                    raise RuntimeError(f"projection harness mismatch: {target}")
                backup = self.root / f".{directory_name}.old-{uuid.uuid4().hex}"
                target.rename(backup)
            stage.rename(target)
            if backup is not None:
                _remove_owned_tree(self.root, backup)
                backup = None
        except Exception:
            if stage.exists():
                _remove_owned_tree(self.root, stage)
            if backup is not None and backup.exists() and not target.exists():
                backup.rename(target)
            raise

        projected = {
            "item_id": item_id,
            "harness": self.harness,
            "name": entry["title"],
            "source": entry["source"],
            "state": entry["state"],
            "canonical_path": str(skill_path),
            "directory": directory_name,
            "path": str(target),
            "files": len(files),
            "bytes": total,
            "skipped_symlinks": skipped,
            "projected_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest["skills"][item_id] = projected
        self._write_manifest(manifest)
        return projected

    def list(self) -> dict[str, Any]:
        manifest = self._manifest()
        rows = []
        for item_id, row in sorted(manifest["skills"].items()):
            if not isinstance(row, dict):
                continue
            path = _inside(self.root, self.root / str(row.get("directory") or ""))
            rows.append({**row, "item_id": item_id, "available": path.is_dir()})
        return {
            "root": str(self.root),
            "harness": self.harness,
            "skills": rows,
            "reload": _reload_command(self.harness),
        }

    def clear(self, skill_ids: list[str] | None = None) -> dict[str, Any]:
        manifest = self._manifest()
        known = manifest["skills"]
        selected = list(dict.fromkeys(skill_ids or list(known)))
        missing = [item_id for item_id in selected if item_id not in known]
        if missing:
            raise ValueError(f"unknown projected skill IDs: {', '.join(missing)}")
        removed = []
        for item_id in selected:
            row = known[item_id]
            target = _inside(self.root, self.root / str(row["directory"]))
            if target.is_dir():
                try:
                    marker = json.loads(
                        (target / _MARKER_NAME).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"refusing to clear unmarked projection: {target}"
                    ) from exc
                if marker.get("item_id") != item_id:
                    raise RuntimeError(
                        f"refusing to clear projection with mismatched owner: {target}"
                    )
                if marker.get("harness") != self.harness:
                    raise RuntimeError(
                        f"refusing to clear projection from another harness: {target}"
                    )
                _remove_owned_tree(self.root, target)
            removed.append(item_id)
            known.pop(item_id)
        self._write_manifest(manifest)
        return {
            "harness": self.harness,
            "removed": removed,
            "remaining": len(known),
            "reload": _reload_command(self.harness),
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid YAML configuration: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"configuration root must be a mapping: {path}")
    return payload


def _write_yaml_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    rendered = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if path.is_file() and path.read_text(encoding="utf-8") == rendered:
        return False
    _atomic_text(path, rendered)
    return True


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid JSON configuration: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"configuration root must be an object: {path}")
    return payload


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == rendered:
        return False
    _atomic_text(path, rendered)
    return True


def _prepare_scout_profile(
    settings: Settings,
    base_config: dict[str, Any],
) -> dict[str, Any]:
    """Create a Retrieval-owned OMP profile containing no MCP configuration."""

    profile_name = settings.scout_profile.strip()
    if not profile_name:
        raise RuntimeError(
            "RETRIEVAL_SCOUT_PROFILE must name an isolated OMP profile"
        )
    omp_home = settings.omp_config.parent.parent
    profile_root = omp_home / "profiles" / profile_name / "agent"
    marker = profile_root / ".retrieval-scout.json"
    legacy_marker = profile_root / ".hermes-retrieval-scout.json"
    profile_root.mkdir(parents=True, exist_ok=True)
    if legacy_marker.is_file() and not marker.is_file():
        try:
            legacy = json.loads(legacy_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"invalid legacy scout marker: {legacy_marker}") from exc
        if legacy.get("owner") != "hermes-retrieval":
            raise RuntimeError(f"refusing unowned legacy scout profile: {profile_root}")
        _atomic_text(
            marker,
            json.dumps(
                {"owner": "retrieval", "profile": profile_name},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        legacy_marker.unlink()
    if any(profile_root.iterdir()) and not marker.is_file():
        raise RuntimeError(
            f"refusing to adopt an existing OMP profile: {profile_root}"
        )
    _atomic_text(
        marker,
        json.dumps(
            {"owner": "retrieval", "profile": profile_name},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )

    omp_command = shlex.split(settings.omp_command)
    if not omp_command:
        raise RuntimeError("RETRIEVAL_OMP_COMMAND is empty")
    initialized = subprocess.run(
        [*omp_command, f"--profile={profile_name}", "config", "path"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if initialized.returncode:
        raise RuntimeError(
            "could not initialize the isolated OMP scout profile: "
            f"{initialized.stderr[-1000:]}"
        )

    roles = base_config.get("modelRoles")
    roles = dict(roles) if isinstance(roles, dict) else {}
    if settings.scout_model:
        roles["default"] = settings.scout_model
    default_model = str(roles.get("default") or "")
    provider = default_model.split("/", 1)[0] if "/" in default_model else ""
    profile_config = {
        "modelRoles": roles,
        "startup": {"checkUpdate": False},
        "advisor": {"enabled": False, "subagents": False},
        "exa": {
            "enabled": False,
            "enableSearch": False,
            "enableResearcher": False,
            "enableWebsets": False,
        },
        "mcp": {"enableProjectConfig": False},
        "skills": {
            "enableSkillCommands": False,
            "customDirectories": [],
        },
        "marketplace": {"autoUpdate": "off"},
    }
    _write_yaml_if_changed(profile_root / "config.yml", profile_config)
    _atomic_text(profile_root / "mcp.json", '{"mcpServers": {}}\n')

    base_models_yaml = settings.omp_config.parent / "models.yml"
    if base_models_yaml.is_file():
        _atomic_text(
            profile_root / "models.yml",
            base_models_yaml.read_text(encoding="utf-8"),
        )

    base_models_db = settings.omp_config.parent / "models.db"
    profile_models_db = profile_root / "models.db"
    cached_models = 0
    if base_models_db.is_file():
        for suffix in ("-wal", "-shm"):
            Path(f"{profile_models_db}{suffix}").unlink(missing_ok=True)
        with sqlite3.connect(
            f"file:{base_models_db}?mode=ro", uri=True
        ) as source_db, sqlite3.connect(profile_models_db) as target_db:
            source_db.backup(target_db)
            if provider:
                target_db.execute(
                    "DELETE FROM model_cache WHERE provider_id NOT LIKE ?",
                    (f"{provider}%",),
                )
            cached_models = int(
                target_db.execute("SELECT COUNT(*) FROM model_cache").fetchone()[0]
            )
            target_db.commit()

    base_agent_db = settings.omp_config.parent / "agent.db"
    profile_agent_db = profile_root / "agent.db"
    credentials = 0
    if provider and base_agent_db.is_file() and profile_agent_db.is_file():
        with sqlite3.connect(
            f"file:{base_agent_db}?mode=ro", uri=True
        ) as source_db, sqlite3.connect(profile_agent_db) as target_db:
            source_columns = {
                str(row[1])
                for row in source_db.execute("PRAGMA table_info(auth_credentials)")
            }
            target_columns = {
                str(row[1])
                for row in target_db.execute("PRAGMA table_info(auth_credentials)")
            }
            columns = [
                name
                for name in (
                    "provider",
                    "credential_type",
                    "data",
                    "disabled_cause",
                    "identity_key",
                    "created_at",
                    "updated_at",
                )
                if name in source_columns and name in target_columns
            ]
            target_db.execute(
                "DELETE FROM auth_credentials WHERE provider = ?", (provider,)
            )
            placeholders = ", ".join("?" for _ in columns)
            names = ", ".join(columns)
            rows = source_db.execute(
                f"SELECT {names} FROM auth_credentials WHERE provider = ?",
                (provider,),
            ).fetchall()
            if rows:
                target_db.executemany(
                    f"INSERT INTO auth_credentials ({names}) VALUES ({placeholders})",
                    rows,
                )
            target_db.commit()
            credentials = len(rows)

    return {
        "profile": profile_name,
        "path": str(profile_root),
        "model": default_model,
        "provider": provider,
        "credentials_copied": credentials,
        "model_cache_rows": cached_models,
        "mcp_servers": 0,
        "sandbox_home": str(settings.scout_home),
    }


def _without_legacy_roots(values: list[Any], *, keep: str) -> list[Any]:
    legacy = {
        str(
            Path("~/.local/share/hermes-retrieval/projections/skills")
            .expanduser()
            .resolve()
        ),
        str((Path("~/.local/share/retrieval/projections/skills").expanduser().resolve())),
    }
    return [value for value in values if str(value) == keep or str(value) not in legacy]


def integrate_harnesses(settings: Settings) -> dict[str, Any]:
    """Register isolated projection and MCP lanes for Hermes and OMP."""

    hermes_projection = SkillProjection(settings, "hermes")
    omp_projection = SkillProjection(settings, "omp")
    hermes_root = str(hermes_projection.root)
    omp_root = str(omp_projection.root)
    hermes_projection.root.mkdir(parents=True, exist_ok=True)
    omp_projection.root.mkdir(parents=True, exist_ok=True)

    hermes_changed = False
    if settings.hermes_config.is_file():
        hermes = _load_yaml(settings.hermes_config)
        hermes_skills = hermes.setdefault("skills", {})
        if not isinstance(hermes_skills, dict):
            raise RuntimeError("Hermes skills configuration must be a mapping")
        external = hermes_skills.setdefault("external_dirs", [])
        if not isinstance(external, list):
            raise RuntimeError("Hermes skills.external_dirs must be a list")
        external[:] = [
            value
            for value in _without_legacy_roots(external, keep=hermes_root)
            if str(value) != omp_root
        ]
        if hermes_root not in external:
            external.append(hermes_root)
        mcp_servers = hermes.setdefault("mcp_servers", {})
        if not isinstance(mcp_servers, dict):
            raise RuntimeError("Hermes mcp_servers must be a mapping")
        server = mcp_servers.setdefault("retrieval", {})
        if not isinstance(server, dict):
            raise RuntimeError("Hermes retrieval MCP configuration must be a mapping")
        server.setdefault("command", str(settings.root / ".venv" / "bin" / "python"))
        server.setdefault("args", ["-m", "hermes_retrieval.server"])
        server.setdefault("connect_timeout", 120.0)
        server["enabled"] = True
        environment = server.setdefault("env", {})
        if not isinstance(environment, dict):
            raise RuntimeError("Hermes retrieval MCP env must be a mapping")
        environment["RETRIEVAL_HARNESS"] = "hermes"
        hermes_changed = _write_yaml_if_changed(settings.hermes_config, hermes)

    omp_changed = False
    scout_profile: dict[str, Any] | None = None
    if settings.omp_config.is_file():
        omp = _load_yaml(settings.omp_config)
        omp_skills = omp.setdefault("skills", {})
        if not isinstance(omp_skills, dict):
            raise RuntimeError("OMP skills configuration must be a mapping")
        custom = omp_skills.setdefault("customDirectories", [])
        if not isinstance(custom, list):
            raise RuntimeError("OMP skills.customDirectories must be a list")
        custom[:] = [
            value
            for value in _without_legacy_roots(custom, keep=omp_root)
            if str(value) != hermes_root
        ]
        if omp_root not in custom:
            custom.append(omp_root)
        omp_changed = _write_yaml_if_changed(settings.omp_config, omp)
        scout_profile = _prepare_scout_profile(settings, omp)

    omp_mcp_changed = False
    if settings.omp_config.is_file():
        omp_mcp = _load_json(settings.omp_mcp_config)
        omp_mcp.setdefault(
            "$schema",
            "https://raw.githubusercontent.com/can1357/oh-my-pi/main/"
            "packages/coding-agent/src/config/mcp-schema.json",
        )
        servers = omp_mcp.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise RuntimeError("OMP mcpServers must be an object")
        server = servers.setdefault("retrieval", {})
        if not isinstance(server, dict):
            raise RuntimeError("OMP retrieval MCP configuration must be an object")
        server.setdefault("type", "stdio")
        server.setdefault("command", str(settings.root / "start.sh"))
        server.setdefault("cwd", str(settings.root))
        environment = server.setdefault("env", {})
        if not isinstance(environment, dict):
            raise RuntimeError("OMP retrieval MCP env must be an object")
        environment["RETRIEVAL_HARNESS"] = "omp"
        omp_mcp_changed = _write_json_if_changed(settings.omp_mcp_config, omp_mcp)

    return {
        "projection_root": str(settings.projection_root),
        "hermes": {
            "config": str(settings.hermes_config),
            "available": settings.hermes_config.is_file(),
            "changed": hermes_changed,
            "projection": hermes_root,
            "reload": "/reload-skills",
        },
        "omp": {
            "config": str(settings.omp_config),
            "mcp_config": str(settings.omp_mcp_config),
            "available": settings.omp_config.is_file(),
            "changed": omp_changed or omp_mcp_changed,
            "projection": omp_root,
            "reload": "/reload",
            "scout_profile": scout_profile,
        },
        "restart_required": hermes_changed or omp_changed or omp_mcp_changed,
    }
