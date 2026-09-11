from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
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
    """Read the installed Hermes projection and OMP inline contract."""

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
    skill_source = root / "skills" / "retrieve-knowledge"
    skill_files = (Path("SKILL.md"), Path("agents") / "openai.yaml")

    for path, name in (
        (python, "virtual-environment Python"),
        (start, "MCP launcher"),
        (hook, "Hermes session-close worker"),
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
        native = _native_hermes_state(settings, home, desired_hook["command"])

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
            check(
                scope,
                "native registries",
                native["ok"]
                and native["mcp_enabled"]
                and native["tools_enabled"]
                and native["hook_allowed"],
                native["detail"],
            )
            check(
                scope,
                "hook consent",
                _hook_consent_current(
                    home,
                    "on_session_end",
                    desired_hook["command"],
                    hook,
                ),
                str(home / "shell-hooks-allowlist.json"),
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
            check(
                scope,
                "isolated native registry boundary",
                native["ok"]
                and not native["mcp_present"]
                and not native["tools_present"]
                and not native["hook_present"],
                native["detail"],
            )
            check(
                scope,
                "isolated hook consent boundary",
                not _hook_consent_present(
                    home,
                    "on_session_end",
                    desired_hook["command"],
                ),
                str(home / "shell-hooks-allowlist.json"),
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
    omp_projection_path = Path(omp_projection)
    check(
        "omp",
        "no package projection storage",
        not omp_projection_path.exists() or not any(omp_projection_path.iterdir()),
        omp_projection,
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
                and server["env"].get("RETRIEVAL_DELIVERY") == "inline"
            )
            check(scope, "MCP", valid_server, str(mcp_path))
            check(
                scope,
                "turn-local delivery",
                not isinstance(custom, list) or omp_projection not in custom,
                omp_projection,
            )
            check(
                scope,
                "no session-close extension",
                not extension.exists(),
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


def _native_hermes_state(
    settings: Settings,
    home: Path,
    hook_command: str,
) -> dict[str, Any]:
    executable = shlex.split(settings.hermes_command)
    outputs: dict[str, str] = {}
    failures: list[str] = []
    for name, arguments in (
        ("mcp", ["mcp", "list"]),
        ("tools", ["tools", "list"]),
        ("hooks", ["hooks", "list"]),
    ):
        try:
            result = subprocess.run(
                [*executable, *arguments],
                cwd=str(settings.root),
                env={**os.environ, "HERMES_HOME": str(home)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            outputs[name] = ""
            failures.append(f"{name}: {exc}")
            continue
        outputs[name] = f"{result.stdout}\n{result.stderr}"
        if result.returncode:
            failures.append(f"{name}: exit {result.returncode}")

    mcp_present = bool(re.search(r"(?m)^\s*retrieval\s+", outputs.get("mcp", "")))
    mcp_enabled = bool(
        re.search(
            r"(?m)^\s*retrieval\s+.*\u2713\s+enabled\s*$",
            outputs.get("mcp", ""),
        )
    )
    tools_present = bool(
        re.search(r"(?m)^\s*retrieval\s+", outputs.get("tools", ""))
    )
    tools_enabled = bool(
        re.search(
            r"(?m)^\s*retrieval\s+all tools enabled\s*$",
            outputs.get("tools", ""),
        )
    )
    hook_lines = [
        line
        for line in outputs.get("hooks", "").splitlines()
        if hook_command in line
    ]
    return {
        "ok": not failures,
        "mcp_present": mcp_present,
        "mcp_enabled": mcp_enabled,
        "tools_present": tools_present,
        "tools_enabled": tools_enabled,
        "hook_present": len(hook_lines) == 1,
        "hook_allowed": len(hook_lines) == 1 and "\u2713 allowed" in hook_lines[0],
        "detail": "; ".join(failures) or "hermes mcp/tools/hooks list",
    }


def _hook_consent_entry(
    home: Path,
    event: str,
    command: str,
) -> dict[str, Any] | None:
    path = home / "shell-hooks-allowlist.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    approvals = payload.get("approvals") if isinstance(payload, dict) else None
    if not isinstance(approvals, list):
        return None
    for entry in approvals:
        if (
            isinstance(entry, dict)
            and entry.get("event") == event
            and entry.get("command") == command
        ):
            return entry
    return None


def _hook_consent_present(home: Path, event: str, command: str) -> bool:
    return _hook_consent_entry(home, event, command) is not None


def _hook_consent_current(
    home: Path,
    event: str,
    command: str,
    executable: Path,
) -> bool:
    entry = _hook_consent_entry(home, event, command)
    approved = entry.get("script_mtime_at_approval") if entry else None
    if not isinstance(approved, str) or not executable.is_file():
        return False
    try:
        approved_at = datetime.fromisoformat(approved.replace("Z", "+00:00"))
    except ValueError:
        return False
    if approved_at.tzinfo is None:
        approved_at = approved_at.replace(tzinfo=timezone.utc)
    approved_milliseconds = int(approved_at.timestamp() * 1000)
    current_milliseconds = int(executable.stat().st_mtime * 1000)
    return approved_milliseconds == current_milliseconds
