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
        "compaction": {
            "remoteEnabled": False,
            "remoteStreamingV2Enabled": False,
        },
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


def _run_harness_command(
    command: str,
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> None:
    executable = shlex.split(command)
    if not executable:
        raise RuntimeError("harness command is empty")
    env = os.environ.copy()
    env.update(environment or {})
    result = subprocess.run(
        [*executable, *arguments],
        cwd=str(Path.home()),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(
            f"{' '.join([*executable, *arguments])} failed ({result.returncode}): {detail}"
        )


def _hermes_python(settings: Settings) -> Path:
    command = shlex.split(settings.hermes_command)
    if not command:
        raise RuntimeError("Hermes command is empty")
    launcher_name = shutil.which(command[0]) or command[0]
    launcher = Path(launcher_name).expanduser().resolve(strict=False)
    if not launcher.is_file():
        raise RuntimeError(f"Hermes launcher was not found: {launcher}")
    first_line = launcher.read_text(encoding="utf-8").splitlines()[0]
    if not first_line.startswith("#!"):
        raise RuntimeError(f"Hermes launcher has no interpreter shebang: {launcher}")
    # Keep the venv-facing interpreter path from the launcher. Resolving the
    # python3 symlink to the host interpreter drops the venv's site-packages.
    interpreter = Path(first_line[2:].strip()).expanduser()
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise RuntimeError(f"Hermes Python runtime was not found: {interpreter}")
    return interpreter


def _hook_approval_present(home: Path, event: str, command: str) -> bool:
    allowlist = home / "shell-hooks-allowlist.json"
    if not allowlist.is_file():
        return False
    try:
        payload = json.loads(allowlist.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Hermes hook allowlist is invalid: {allowlist}: {exc}") from exc
    approvals = payload.get("approvals") if isinstance(payload, dict) else None
    if approvals is None:
        return False
    if not isinstance(approvals, list):
        raise RuntimeError(f"Hermes hook approvals must be a sequence: {allowlist}")
    return any(
        isinstance(row, dict)
        and row.get("event") == event
        and row.get("command") == command
        for row in approvals
    )


def _approve_owned_hermes_hook(
    settings: Settings,
    home: Path,
    event: str,
    command: str,
) -> bool:
    """Refresh consent for Retrieval's exact hook without blanket auto-approval."""

    code = "\n".join(
        (
            "import sys",
            "from agent import shell_hooks",
            "event, command = sys.argv[1:3]",
            "entry = shell_hooks.allowlist_entry_for(event, command)",
            "mtime = shell_hooks.script_mtime_iso(command)",
            "changed = entry is None or entry.get('script_mtime_at_approval') != mtime",
            "if changed:",
            "    shell_hooks._record_approval(event, command)",
            "print('changed' if changed else 'unchanged')",
        )
    )
    result = subprocess.run(
        [str(_hermes_python(settings)), "-c", code, event, command],
        cwd=str(settings.root),
        env={**os.environ, "HERMES_HOME": str(home)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(
            f"Hermes could not approve Retrieval's exact hook for {home}: {detail}"
        )
    return result.stdout.strip() == "changed"


def _revoke_owned_hermes_hook(
    settings: Settings,
    home: Path,
    event: str,
    command: str,
) -> bool:
    if not _hook_approval_present(home, event, command):
        return False
    _run_harness_command(
        settings.hermes_command,
        ["hooks", "revoke", command],
        environment={"HERMES_HOME": str(home)},
    )
    return True


def _all_hermes_profile_homes(settings: Settings) -> list[Path]:
    homes = [settings.hermes_config.parent]
    profiles = settings.hermes_config.parent / "profiles"
    if profiles.is_dir():
        homes.extend(
            entry
            for entry in sorted(profiles.iterdir())
            if entry.is_dir() and (entry / "config.yaml").is_file()
        )
    return homes


def _is_private_librarian_home(home: Path) -> bool:
    config_path = home / "config.yaml"
    if not config_path.is_file():
        return False
    servers = _load_yaml(config_path).get("mcp_servers")
    return isinstance(servers, dict) and "librarian-okf" in servers


def _hermes_profile_homes(settings: Settings) -> list[Path]:
    default = settings.hermes_config.parent.resolve(strict=False)
    return [
        home
        for home in _all_hermes_profile_homes(settings)
        if home.resolve(strict=False) == default or not _is_private_librarian_home(home)
    ]


def _all_omp_profile_homes(settings: Settings) -> list[Path]:
    homes = [settings.omp_config.parent]
    profiles = settings.omp_config.parent.parent / "profiles"
    if profiles.is_dir():
        homes.extend(
            entry / "agent"
            for entry in sorted(profiles.iterdir())
            if entry.is_dir()
            and entry.name != settings.scout_profile
            and (entry / "agent" / "config.yml").is_file()
        )
    return homes


def _omp_profile_homes(settings: Settings) -> list[Path]:
    """Return OMP homes using the ordinary external-tool stack.

    The Retrieval scout, empty auditor profiles, and Librarian's private OKF
    worker stay isolated. A normal profile is recognized from its existing
    Retrieval or Camofox MCP registration rather than from its display name.
    """

    homes = [settings.omp_config.parent]
    for home in _all_omp_profile_homes(settings)[1:]:
        mcp_path = home / "mcp.json"
        if not mcp_path.is_file():
            continue
        servers = _load_json(mcp_path).get("mcpServers")
        if isinstance(servers, dict) and (
            "retrieval" in servers or "camofox" in servers
        ):
            homes.append(home)
    return homes


def managed_profile_homes(settings: Settings, harness: str) -> list[Path]:
    """Return only homes that participate in Retrieval's ordinary tool stack."""

    if harness == "hermes":
        return _hermes_profile_homes(settings)
    if harness == "omp":
        return _omp_profile_homes(settings)
    raise ValueError(f"unsupported retrieval harness: {harness}")


def _remove_managed_routing_skill(target: Path) -> bool:
    """Remove Retrieval's exact router path from an excluded profile.

    Older Retrieval releases installed earlier revisions of this file, so an
    equality check against the current source leaves stale routers behind.
    The destination path is fixed and the frontmatter name proves ownership;
    unrelated skills are therefore left alone.
    """

    skill_target = target / "SKILL.md"
    if not skill_target.is_file():
        return False

    text = skill_target.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\s*\Z)", text, re.DOTALL)
    if frontmatter is None:
        return False
    try:
        metadata = yaml.safe_load(frontmatter.group(1)) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(metadata, dict) or metadata.get("name") != "retrieve-knowledge":
        return False

    shutil.rmtree(target)
    return True


def _install_routing_skills(
    settings: Settings,
    *,
    hermes_homes: list[Path],
    omp_homes: list[Path],
) -> tuple[dict[str, list[str]], bool]:
    source = settings.root / "skills" / "retrieve-knowledge"
    skill = source / "SKILL.md"
    metadata = source / "agents" / "openai.yaml"
    if not skill.is_file() or not metadata.is_file():
        raise RuntimeError(f"Retrieval routing skill is incomplete: {source}")

    targets = [
        home / "skills" / "retrieve-knowledge"
        for home in omp_homes
    ]
    all_omp_targets = [settings.omp_config.parent / "skills" / "retrieve-knowledge"]
    omp_profiles = settings.omp_config.parent.parent / "profiles"
    if omp_profiles.is_dir():
        all_omp_targets.extend(
            entry / "agent" / "skills" / "retrieve-knowledge"
            for entry in sorted(omp_profiles.iterdir())
            if entry.is_dir()
        )
    targets.extend(
        home / "skills" / "research" / "retrieve-knowledge"
        for home in hermes_homes
    )
    all_hermes_targets = [
        home / "skills" / "research" / "retrieve-knowledge"
        for home in _all_hermes_profile_homes(settings)
    ]

    changed = False
    installed: list[str] = []
    skill_text = skill.read_text(encoding="utf-8")
    metadata_text = metadata.read_text(encoding="utf-8")
    desired = {target.resolve(strict=False) for target in targets}
    removed: list[str] = []
    for target in [*all_omp_targets, *all_hermes_targets]:
        if target.resolve(strict=False) in desired:
            continue
        if _remove_managed_routing_skill(target):
            changed = True
            removed.append(str(target))
    for target in targets:
        skill_target = target / "SKILL.md"
        metadata_target = target / "agents" / "openai.yaml"
        before_skill = skill_target.read_text(encoding="utf-8") if skill_target.is_file() else None
        before_metadata = metadata_target.read_text(encoding="utf-8") if metadata_target.is_file() else None
        _atomic_text(skill_target, skill_text)
        _atomic_text(metadata_target, metadata_text)
        changed = changed or before_skill != skill_text or before_metadata != metadata_text
        installed.append(str(target))
    return {"installed": installed, "removed_from_isolated_profiles": removed}, changed


def _install_lifecycle_integrations(
    settings: Settings,
    *,
    hermes_homes: list[Path],
    omp_homes: list[Path],
) -> tuple[dict[str, Any], bool]:
    hook_script = settings.root / "hooks" / "session-close.sh"
    omp_source = settings.root / "integrations" / "omp" / "retrieval-session-close.ts"
    if not hook_script.is_file() or not omp_source.is_file():
        raise RuntimeError("Retrieval session-close integrations are incomplete")

    changed = False
    hermes_installed: list[str] = []
    hermes_removed: list[str] = []
    hermes_consented: list[str] = []
    hermes_consent_removed: list[str] = []
    command = f"{hook_script} hermes"
    desired_hermes = {home.resolve(strict=False) for home in hermes_homes}
    for home in _all_hermes_profile_homes(settings):
        config_path = home / "config.yaml"
        if not config_path.is_file():
            continue
        payload = _load_yaml(config_path)
        hooks = payload.get("hooks")
        hooks = dict(hooks) if isinstance(hooks, dict) else {}
        current = hooks.get("on_session_end")
        rows = list(current) if isinstance(current, list) else []
        desired = {
            "id": "retrieval-session-close",
            "type": "command",
            "command": command,
            "timeout": 10,
        }
        filtered = [
            row
            for row in rows
            if not (isinstance(row, dict) and row.get("id") == desired["id"])
        ]
        managed = home.resolve(strict=False) in desired_hermes
        if managed:
            filtered.append(desired)
        if filtered != rows:
            _run_harness_command(
                settings.hermes_command,
                ["config", "set", "--force", "hooks.on_session_end", json.dumps(filtered)],
                environment={"HERMES_HOME": str(home)},
            )
            changed = True
        if managed:
            hermes_installed.append(str(config_path))
            if _approve_owned_hermes_hook(
                settings,
                home,
                "on_session_end",
                command,
            ):
                changed = True
            hermes_consented.append(str(home / "shell-hooks-allowlist.json"))
        elif len(filtered) != len(rows):
            hermes_removed.append(str(config_path))
        if not managed and _revoke_owned_hermes_hook(
            settings,
            home,
            "on_session_end",
            command,
        ):
            changed = True
            hermes_consent_removed.append(str(home / "shell-hooks-allowlist.json"))

    omp_installed: list[str] = []
    omp_removed: list[str] = []
    extension_text = omp_source.read_text(encoding="utf-8")
    desired_omp = {home.resolve(strict=False) for home in omp_homes}
    for home in _all_omp_profile_homes(settings):
        target = home / "extensions" / "retrieval-session-close.ts"
        if home.resolve(strict=False) not in desired_omp:
            if target.is_file() and target.read_text(encoding="utf-8") == extension_text:
                target.unlink()
                changed = True
                omp_removed.append(str(target))
            continue
        before = target.read_text(encoding="utf-8") if target.is_file() else None
        _atomic_text(target, extension_text)
        changed = changed or before != extension_text
        omp_installed.append(str(target))

    return {
        "hermes_hooks": hermes_installed,
        "hermes_hooks_removed_from_isolated_profiles": hermes_removed,
        "hermes_hook_consents": hermes_consented,
        "hermes_hook_consents_removed_from_isolated_profiles": hermes_consent_removed,
        "omp_extensions": omp_installed,
        "omp_extensions_removed_from_isolated_profiles": omp_removed,
        "worker": str(hook_script),
    }, changed


def _configure_hermes_home(
    settings: Settings,
    home: Path,
    projection_root: str,
) -> dict[str, Any]:
    config_path = home / "config.yaml"
    if not config_path.is_file():
        return {"config": str(config_path), "available": False, "changed": False}
    config = _load_yaml(config_path)
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise RuntimeError(f"Hermes skills configuration must be a mapping: {config_path}")
    external = skills.setdefault("external_dirs", [])
    if not isinstance(external, list):
        raise RuntimeError(f"Hermes skills.external_dirs must be a list: {config_path}")
    previous_external = list(external)
    external[:] = [value for value in external if str(value) != projection_root]
    external.append(projection_root)

    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"Hermes mcp_servers must be a mapping: {config_path}")
    server = servers.setdefault("retrieval", {})
    if not isinstance(server, dict):
        raise RuntimeError(f"Hermes retrieval MCP configuration must be a mapping: {config_path}")
    previous_server = json.loads(json.dumps(server))
    server["command"] = str(settings.root / ".venv" / "bin" / "python")
    server["args"] = ["-m", "hermes_retrieval.server"]
    server["connect_timeout"] = 120.0
    server["enabled"] = True
    environment = server.setdefault("env", {})
    if not isinstance(environment, dict):
        raise RuntimeError(f"Hermes retrieval MCP env must be a mapping: {config_path}")
    environment["RETRIEVAL_HARNESS"] = "hermes"
    changed = previous_external != external or previous_server != server
    if changed:
        scoped_environment = {"HERMES_HOME": str(home)}
        _run_harness_command(
            settings.hermes_command,
            ["config", "set", "--force", "skills.external_dirs", json.dumps(external)],
            environment=scoped_environment,
        )
        _run_harness_command(
            settings.hermes_command,
            ["config", "set", "--force", "mcp_servers.retrieval", json.dumps(server)],
            environment=scoped_environment,
        )
    return {"config": str(config_path), "available": True, "changed": changed}


def _remove_retrieval_from_hermes_home(
    settings: Settings,
    home: Path,
    projection_root: str,
) -> dict[str, Any]:
    """Remove only Retrieval-owned settings from an isolated Hermes home."""

    config_path = home / "config.yaml"
    if not config_path.is_file():
        return {"config": str(config_path), "available": False, "changed": False}
    config = _load_yaml(config_path)
    scoped_environment = {"HERMES_HOME": str(home)}
    changed = False

    servers = config.get("mcp_servers")
    if isinstance(servers, dict) and "retrieval" in servers:
        _run_harness_command(
            settings.hermes_command,
            ["config", "unset", "mcp_servers.retrieval"],
            environment=scoped_environment,
        )
        changed = True

    skills = config.get("skills")
    external = skills.get("external_dirs") if isinstance(skills, dict) else None
    if isinstance(external, list):
        desired = [value for value in external if str(value) != projection_root]
        if desired != external:
            arguments = (
                ["config", "set", "--force", "skills.external_dirs", json.dumps(desired)]
                if desired
                else ["config", "unset", "skills.external_dirs"]
            )
            _run_harness_command(
                settings.hermes_command,
                arguments,
                environment=scoped_environment,
            )
            changed = True
    return {"config": str(config_path), "available": True, "changed": changed}


def _configure_omp_home(
    settings: Settings,
    home: Path,
    projection_root: str,
) -> dict[str, Any]:
    config_path = home / "config.yml"
    mcp_path = home / "mcp.json"
    if not config_path.is_file():
        return {
            "config": str(config_path),
            "mcp_config": str(mcp_path),
            "available": False,
            "changed": False,
        }
    config = _load_yaml(config_path)
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise RuntimeError(f"OMP skills configuration must be a mapping: {config_path}")
    custom = skills.setdefault("customDirectories", [])
    if not isinstance(custom, list):
        raise RuntimeError(f"OMP skills.customDirectories must be a list: {config_path}")
    previous_custom = list(custom)
    custom[:] = [value for value in custom if str(value) != projection_root]
    custom.append(projection_root)
    config_changed = previous_custom != custom
    if config_changed:
        _run_harness_command(
            settings.omp_command,
            ["config", "set", "skills.customDirectories", json.dumps(custom)],
            environment={"PI_CODING_AGENT_DIR": str(home)},
        )

    mcp = _load_json(mcp_path)
    mcp.setdefault(
        "$schema",
        "https://raw.githubusercontent.com/can1357/oh-my-pi/main/"
        "packages/coding-agent/src/config/mcp-schema.json",
    )
    servers = mcp.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"OMP mcpServers must be an object: {mcp_path}")
    server = servers.setdefault("retrieval", {})
    if not isinstance(server, dict):
        raise RuntimeError(f"OMP retrieval MCP configuration must be an object: {mcp_path}")
    server["type"] = "stdio"
    server["command"] = str(settings.root / "start.sh")
    server["cwd"] = str(settings.root)
    environment = server.setdefault("env", {})
    if not isinstance(environment, dict):
        raise RuntimeError(f"OMP retrieval MCP env must be an object: {mcp_path}")
    environment["RETRIEVAL_HARNESS"] = "omp"
    mcp_changed = _write_json_if_changed(mcp_path, mcp)
    return {
        "config": str(config_path),
        "mcp_config": str(mcp_path),
        "available": True,
        "changed": config_changed or mcp_changed,
    }


def _remove_retrieval_from_omp_home(
    settings: Settings,
    home: Path,
    projection_root: str,
) -> dict[str, Any]:
    """Remove only Retrieval-owned settings from an isolated OMP home."""

    config_path = home / "config.yml"
    mcp_path = home / "mcp.json"
    changed = False
    if config_path.is_file():
        config = _load_yaml(config_path)
        skills = config.get("skills")
        custom = skills.get("customDirectories") if isinstance(skills, dict) else None
        if isinstance(custom, list):
            desired = [value for value in custom if str(value) != projection_root]
            if desired != custom:
                _run_harness_command(
                    settings.omp_command,
                    ["config", "set", "skills.customDirectories", json.dumps(desired)],
                    environment={"PI_CODING_AGENT_DIR": str(home)},
                )
                changed = True
    if mcp_path.is_file():
        mcp = _load_json(mcp_path)
        servers = mcp.get("mcpServers")
        if isinstance(servers, dict) and "retrieval" in servers:
            del servers["retrieval"]
            changed = _write_json_if_changed(mcp_path, mcp) or changed
    return {
        "config": str(config_path),
        "mcp_config": str(mcp_path),
        "available": config_path.is_file(),
        "changed": changed,
    }


def integrate_harnesses(settings: Settings) -> dict[str, Any]:
    """Register isolated projection and MCP lanes for Hermes and OMP."""

    hermes_projection = SkillProjection(settings, "hermes")
    omp_projection = SkillProjection(settings, "omp")
    hermes_root = str(hermes_projection.root)
    omp_root = str(omp_projection.root)
    hermes_projection.root.mkdir(parents=True, exist_ok=True)
    omp_projection.root.mkdir(parents=True, exist_ok=True)

    hermes_homes = _hermes_profile_homes(settings)
    hermes_profiles = [
        _configure_hermes_home(settings, home, hermes_root)
        for home in hermes_homes
    ]
    managed_hermes = {home.resolve(strict=False) for home in hermes_homes}
    isolated_hermes_profiles = [
        _remove_retrieval_from_hermes_home(settings, home, hermes_root)
        for home in _all_hermes_profile_homes(settings)
        if home.resolve(strict=False) not in managed_hermes
    ]
    hermes_changed = any(
        row["changed"] for row in [*hermes_profiles, *isolated_hermes_profiles]
    )

    omp_homes = _omp_profile_homes(settings)
    omp_profiles = [
        _configure_omp_home(settings, home, omp_root)
        for home in omp_homes
    ]
    managed_omp = {home.resolve(strict=False) for home in omp_homes}
    isolated_omp_profiles = [
        _remove_retrieval_from_omp_home(settings, home, omp_root)
        for home in _all_omp_profile_homes(settings)
        if home.resolve(strict=False) not in managed_omp
    ]
    omp_changed = any(
        row["changed"] for row in [*omp_profiles, *isolated_omp_profiles]
    )
    scout_profile: dict[str, Any] | None = None
    if settings.omp_config.is_file():
        scout_profile = _prepare_scout_profile(
            settings,
            _load_yaml(settings.omp_config),
        )

    installed_skills, skills_changed = _install_routing_skills(
        settings,
        hermes_homes=hermes_homes,
        omp_homes=omp_homes,
    )
    lifecycle, lifecycle_changed = _install_lifecycle_integrations(
        settings,
        hermes_homes=hermes_homes,
        omp_homes=omp_homes,
    )

    return {
        "projection_root": str(settings.projection_root),
        "hermes": {
            "config": str(settings.hermes_config),
            "available": settings.hermes_config.is_file(),
            "changed": hermes_changed,
            "projection": hermes_root,
            "reload": "/reload-skills",
            "profiles": hermes_profiles,
            "isolated_profiles_cleaned": isolated_hermes_profiles,
        },
        "omp": {
            "config": str(settings.omp_config),
            "mcp_config": str(settings.omp_mcp_config),
            "available": settings.omp_config.is_file(),
            "changed": omp_changed,
            "projection": omp_root,
            "reload": "/reload",
            "scout_profile": scout_profile,
            "profiles": omp_profiles,
            "isolated_profiles_cleaned": isolated_omp_profiles,
        },
        "routing_skills": installed_skills,
        "session_close": lifecycle,
        "restart_required": (
            hermes_changed
            or omp_changed
            or skills_changed
            or lifecycle_changed
        ),
    }
