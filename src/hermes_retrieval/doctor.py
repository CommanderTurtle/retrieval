from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .projection import (
    _all_hermes_profile_homes,
    _all_omp_profile_homes,
    _hermes_profile_homes,
    _load_json,
    _load_yaml,
    _omp_profile_homes,
)


def audit_harnesses(settings: Settings) -> dict[str, Any]:
    """Read the installed Hermes/OMP projection contract without changing it."""

    checks: list[dict[str, Any]] = []

    def check(scope: str, name: str, passed: bool, detail: str) -> None:
        checks.append(
            {
                "scope": scope,
                "name": name,
                "passed": bool(passed),
                "detail": detail,
            }
        )

    root = settings.root.resolve(strict=False)
    python = root / ".venv" / "bin" / "python"
    start = root / "start.sh"
    hook = root / "hooks" / "session-close.sh"
    omp_extension = root / "integrations" / "omp" / "retrieval-session-close.ts"
    skill_source = root / "skills" / "retrieve-knowledge"
    skill_files = (Path("SKILL.md"), Path("agents") / "openai.yaml")

    for path, name in (
        (python, "virtual-environment Python"),
        (start, "MCP launcher"),
        (hook, "Hermes session-close worker"),
        (omp_extension, "OMP session-close extension"),
        (skill_source / skill_files[0], "routing skill"),
        (skill_source / skill_files[1], "routing metadata"),
    ):
        check("source", name, path.is_file(), str(path))

    hermes_projection = str(settings.projection_lane_root("hermes"))
    managed_hermes = {
        path.resolve(strict=False) for path in _hermes_profile_homes(settings)
    }
    desired_hook = {
        "id": "retrieval-session-close",
        "type": "command",
        "command": f"{hook} hermes",
        "timeout": 10,
    }
    for home in _all_hermes_profile_homes(settings):
        scope = f"hermes:{_profile_name(home, settings.hermes_config.parent)}"
        managed = home.resolve(strict=False) in managed_hermes
        payload = _load_yaml(home / "config.yaml")
        servers = payload.get("mcp_servers")
        servers = servers if isinstance(servers, dict) else {}
        server = servers.get("retrieval")
        skills = payload.get("skills")
        external = skills.get("external_dirs") if isinstance(skills, dict) else []
        hooks = payload.get("hooks")
        session_end = hooks.get("on_session_end") if isinstance(hooks, dict) else []
        router = home / "skills" / "research" / "retrieve-knowledge"

        if managed:
            valid_server = isinstance(server, dict) and (
                server.get("command") == str(python)
                and server.get("args") == ["-m", "hermes_retrieval.server"]
                and server.get("enabled") is True
                and isinstance(server.get("env"), dict)
                and server["env"].get("RETRIEVAL_HARNESS") == "hermes"
            )
            check(scope, "MCP", valid_server, str(home / "config.yaml"))
            check(
                scope,
                "projection lane",
                isinstance(external, list) and hermes_projection in external,
                hermes_projection,
            )
            check(
                scope,
                "session-close hook",
                isinstance(session_end, list) and desired_hook in session_end,
                str(home / "config.yaml"),
            )
            _check_router(check, scope, skill_source, router, skill_files)
        else:
            check(scope, "isolated MCP boundary", "retrieval" not in servers, str(home))
            check(
                scope,
                "isolated projection boundary",
                not isinstance(external, list) or hermes_projection not in external,
                str(home),
            )
            check(
                scope,
                "isolated hook boundary",
                not isinstance(session_end, list)
                or not any(
                    isinstance(row, dict)
                    and row.get("id") == "retrieval-session-close"
                    for row in session_end
                ),
                str(home),
            )
            check(scope, "isolated router boundary", not router.exists(), str(router))

    omp_projection = str(settings.projection_lane_root("omp"))
    managed_omp = {
        path.resolve(strict=False) for path in _omp_profile_homes(settings)
    }
    omp_homes = _all_omp_profile_homes(settings)
    scout = (
        settings.omp_config.parent.parent
        / "profiles"
        / settings.scout_profile
        / "agent"
    )
    if (scout / "config.yml").is_file() and scout not in omp_homes:
        omp_homes.append(scout)
    desired_extension = (
        omp_extension.read_text(encoding="utf-8")
        if omp_extension.is_file()
        else None
    )
    for home in omp_homes:
        scope = f"omp:{_profile_name(home, settings.omp_config.parent)}"
        managed = home.resolve(strict=False) in managed_omp
        config = _load_yaml(home / "config.yml")
        skills = config.get("skills")
        custom = skills.get("customDirectories") if isinstance(skills, dict) else []
        mcp_path = home / "mcp.json"
        mcp = _load_json(mcp_path)
        servers = mcp.get("mcpServers")
        servers = servers if isinstance(servers, dict) else {}
        server = servers.get("retrieval")
        extension = home / "extensions" / "retrieval-session-close.ts"
        router = home / "skills" / "retrieve-knowledge"

        if managed:
            valid_server = isinstance(server, dict) and (
                server.get("type") == "stdio"
                and server.get("command") == str(start)
                and server.get("cwd") == str(root)
                and isinstance(server.get("env"), dict)
                and server["env"].get("RETRIEVAL_HARNESS") == "omp"
            )
            check(scope, "MCP", valid_server, str(mcp_path))
            check(
                scope,
                "projection lane",
                isinstance(custom, list) and omp_projection in custom,
                omp_projection,
            )
            check(
                scope,
                "session-close extension",
                desired_extension is not None
                and extension.is_file()
                and extension.read_text(encoding="utf-8") == desired_extension,
                str(extension),
            )
            _check_router(check, scope, skill_source, router, skill_files)
        else:
            check(scope, "isolated MCP boundary", "retrieval" not in servers, str(mcp_path))
            check(
                scope,
                "isolated projection boundary",
                not isinstance(custom, list) or omp_projection not in custom,
                str(home / "config.yml"),
            )
            check(
                scope,
                "isolated lifecycle boundary",
                not extension.exists(),
                str(extension),
            )
            check(scope, "isolated router boundary", not router.exists(), str(router))

    failed = sum(not row["passed"] for row in checks)
    return {
        "ok": failed == 0,
        "summary": {
            "checks": len(checks),
            "passed": len(checks) - failed,
            "failed": failed,
        },
        "checks": checks,
    }


def _check_router(
    record: Any,
    scope: str,
    source: Path,
    target: Path,
    files: tuple[Path, ...],
) -> None:
    for relative in files:
        source_file = source / relative
        target_file = target / relative
        passed = (
            source_file.is_file()
            and target_file.is_file()
            and source_file.read_bytes() == target_file.read_bytes()
        )
        record(scope, f"router {relative.as_posix()}", passed, str(target_file))


def _profile_name(home: Path, default: Path) -> str:
    if home.resolve(strict=False) == default.resolve(strict=False):
        return "default"
    return home.parent.name if home.name == "agent" else home.name
