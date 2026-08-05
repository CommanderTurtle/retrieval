from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable


_CONTEXT_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "RULES.md",
    "copilot-instructions.md",
}
_EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "vendor",
}
_HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
_IMPORT = re.compile(r"(?<![\w@])@((?:~|\.{1,2}|/)[^\s`<>()]+)")
_REFERENCE_CUES = re.compile(
    r"\b(architecture|history|rationale|tutorial|runbook|inventory|background|"
    r"migration|decision log|integration notes?|troubleshooting guide)\b",
    re.IGNORECASE,
)
_NATIVE_CUES = re.compile(
    r"\b(must|never|required|requirement|safety|build|test|command|convention|"
    r"do not|path|format|lint|commit)\b",
    re.IGNORECASE,
)


def _provider(path: Path) -> tuple[str, int, Path, bool]:
    parts = path.parts
    name = path.name
    if name == "RULES.md" and path.parent.name == ".omp":
        return "omp-native-rules", 101, path.parent.parent, True
    if name == "AGENTS.md" and path.parent.name == ".omp":
        return "omp-native", 100, path.parent.parent, False
    if name == "CLAUDE.md" and path.parent.name == ".claude":
        return "claude", 80, path.parent.parent, False
    if name == "AGENTS.md" and path.parent.name in {".agent", ".agents"}:
        return "agents", 70, path.parent.parent, False
    if name == "AGENTS.md" and path.parent.name == ".codex":
        return "codex", 70, path.parent.parent, False
    if name == "GEMINI.md" and path.parent.name == ".gemini":
        return "gemini", 60, path.parent.parent, False
    if name == "AGENTS.md" and len(parts) >= 2 and path.parent.name == "opencode":
        return "opencode", 50, path.parent.parent, False
    if name == "copilot-instructions.md" and path.parent.name == ".github":
        return "github", 30, path.parent.parent, False
    if name == "AGENTS.md":
        return "agents-md", 10, path.parent, False
    if name == "CLAUDE.md":
        return "claude-standalone", 80, path.parent, False
    if name == "GEMINI.md":
        return "gemini-standalone", 60, path.parent, False
    return "context", 0, path.parent, False


def _discover(targets: Iterable[Path]) -> tuple[list[Path], list[Path]]:
    roots: list[Path] = []
    files: set[Path] = set()
    for raw in targets:
        target = raw.expanduser().resolve()
        if target.is_file():
            files.add(target)
            roots.append(target.parent)
            continue
        if not target.is_dir():
            raise ValueError(f"context audit target does not exist: {target}")
        roots.append(target)
        for path in target.rglob("*.md"):
            relative = path.relative_to(target)
            if any(part in _EXCLUDED_DIRECTORIES for part in relative.parts):
                continue
            if path.name in _CONTEXT_NAMES:
                files.add(path.resolve())
    return sorted(files), roots


def _sections(text: str) -> list[dict[str, Any]]:
    matches = list(_HEADING.finditer(text))
    rows: list[dict[str, Any]] = []
    if not matches:
        if text.strip():
            rows.append(
                {
                    "heading": "(document)",
                    "level": 0,
                    "start_line": 1,
                    "end_line": max(1, text.count("\n") + 1),
                    "chars": len(text),
                    "estimated_tokens": (len(text) + 3) // 4,
                }
            )
        return rows
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.start() : end]
        start_line = text.count("\n", 0, match.start()) + 1
        end_line = text.count("\n", 0, end) + 1
        rows.append(
            {
                "heading": match.group(2).strip(),
                "level": len(match.group(1)),
                "start_line": start_line,
                "end_line": end_line,
                "chars": len(content),
                "estimated_tokens": (len(content) + 3) // 4,
            }
        )
    return rows


def _without_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]+`", "", text)


def _imports(path: Path, text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for token in dict.fromkeys(_IMPORT.findall(_without_code(text))):
        clean = token.rstrip(".,;:!?)]}\"'")
        if clean.startswith("~"):
            resolved = Path(clean).expanduser()
        elif clean.startswith("/"):
            resolved = Path(clean)
        else:
            resolved = path.parent / clean
        resolved = resolved.resolve(strict=False)
        rows.append(
            {
                "token": f"@{clean}",
                "path": str(resolved),
                "exists": resolved.is_file(),
                "bytes": resolved.stat().st_size if resolved.is_file() else 0,
                "effect": "expands-inline-before-prompt-injection",
            }
        )
    return rows


def _recommendation(path: Path, text: str, sections: list[dict[str, Any]]) -> str:
    if path.name == "RULES.md":
        return "keep-native-sticky"
    if len(text) <= 1800:
        return "keep-native-small"
    reference_weight = sum(
        int(_REFERENCE_CUES.search(str(row["heading"])) is not None)
        * int(row["chars"])
        for row in sections
    )
    native_weight = len(_NATIVE_CUES.findall(text)) * 120
    if len(text) >= 6000 or reference_weight > native_weight:
        return "review-large-sections-for-reference-source"
    return "keep-native-review-manually"


def _baseline_for(
    path: Path,
    roots: list[Path],
    baseline: Path | None,
    file_count: int,
) -> Path | None:
    if baseline is None:
        return None
    baseline = baseline.expanduser().resolve()
    if baseline.is_file():
        return baseline if file_count == 1 else None
    if not baseline.is_dir():
        raise ValueError(f"context baseline does not exist: {baseline}")
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        candidate = baseline / relative
        if candidate.is_file():
            return candidate
    matches = list(baseline.rglob(path.name))
    return matches[0] if len(matches) == 1 else None


def _drift(text: str, baseline_path: Path | None) -> dict[str, Any]:
    if baseline_path is None:
        return {"status": "not-configured-or-unmatched"}
    baseline = baseline_path.read_text(encoding="utf-8", errors="replace")
    before = baseline.splitlines()
    after = text.splitlines()
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            added += j2 - j1
        if tag in {"delete", "replace"}:
            removed += i2 - i1
    return {
        "status": "identical" if before == after else "changed",
        "baseline_path": str(baseline_path),
        "similarity": round(matcher.ratio(), 6),
        "added_lines": added,
        "removed_lines": removed,
    }


def audit_context(
    targets: Iterable[Path],
    *,
    baseline: Path | None = None,
) -> dict[str, Any]:
    """Audit context topology and size without rewriting any instruction file."""

    files, roots = _discover(targets)
    rows: list[dict[str, Any]] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        provider, priority, scope, sticky = _provider(path)
        sections = _sections(text)
        baseline_path = _baseline_for(path, roots, baseline, len(files))
        rows.append(
            {
                "path": str(path),
                "provider": provider,
                "priority": priority,
                "scope": str(scope),
                "sticky": sticky,
                "chars": len(text),
                "bytes": path.stat().st_size,
                "lines": len(text.splitlines()),
                "estimated_tokens": (len(text) + 3) // 4,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "imports": _imports(path, text),
                "sections": sections,
                "large_sections": [row for row in sections if int(row["chars"]) >= 1800],
                "recommendation": _recommendation(path, text, sections),
                "baseline": _drift(text, baseline_path),
                "potential_shadowed_by": [],
            }
        )

    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scope[str(row["scope"])].append(row)
    for scoped in by_scope.values():
        for row in scoped:
            row["potential_shadowed_by"] = [
                other["path"]
                for other in scoped
                if int(other["priority"]) > int(row["priority"])
            ]

    by_hash: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_hash[str(row["sha256"])].append(str(row["path"]))
    duplicates = [paths for paths in by_hash.values() if len(paths) > 1]
    return {
        "read_only": True,
        "targets": [str(path) for path in roots],
        "baseline": str(baseline.expanduser().resolve()) if baseline else None,
        "files": rows,
        "summary": {
            "files": len(rows),
            "chars": sum(int(row["chars"]) for row in rows),
            "estimated_tokens": sum(int(row["estimated_tokens"]) for row in rows),
            "large_files": sum(int(row["chars"]) >= 6000 for row in rows),
            "review_for_reference": sum(
                str(row["recommendation"]).startswith("review-") for row in rows
            ),
            "potential_shadowed": sum(bool(row["potential_shadowed_by"]) for row in rows),
            "exact_duplicate_groups": len(duplicates),
        },
        "exact_duplicates": duplicates,
        "notes": [
            "OMP @path imports expand inline and organize content; they do not reduce prompt size.",
            "Shadowing is a precedence warning; active provider settings and session cwd decide the winner.",
            "There is no universal harness-default AGENTS.md to compare. Drift is reported only against an explicit baseline.",
            "Move only optional rationale/history/runbooks into an explicitly configured references source; keep rules and commands native.",
        ],
    }
