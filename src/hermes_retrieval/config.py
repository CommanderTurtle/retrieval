from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from .models import SourceConfig


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    root: Path
    sources_file: Path
    chroma_host: str
    chroma_port: int
    chroma_ssl: bool
    embedding_url: str
    embedding_model: str
    embedding_api_key: str
    embedding_batch_size: int
    embedding_max_chars: int
    fastembed_model: str
    fastembed_cache: Path
    hermes_command: str
    hermes_session_newer_than: str
    max_skills_per_load: int
    max_skill_chars: int
    max_total_skill_chars: int
    max_recall_chars: int

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        project_root = (root or Path(__file__).resolve().parents[2]).resolve()
        _load_dotenv(project_root / ".env")
        return cls(
            root=project_root,
            sources_file=project_root / "sources.toml",
            chroma_host=os.getenv("RETRIEVAL_CHROMA_HOST", "127.0.0.1"),
            chroma_port=_int_env("RETRIEVAL_CHROMA_PORT", 8100),
            chroma_ssl=_bool_env("RETRIEVAL_CHROMA_SSL"),
            embedding_url=os.getenv("EMBEDDING_URL", "").strip(),
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip(),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", "").strip(),
            embedding_batch_size=max(1, _int_env("EMBEDDING_BATCH_SIZE", 8)),
            embedding_max_chars=max(400, _int_env("EMBEDDING_MAX_CHARS", 4000)),
            fastembed_model=os.getenv(
                "RETRIEVAL_FASTEMBED_MODEL",
                "sentence-transformers/all-MiniLM-L6-v2",
            ),
            fastembed_cache=Path(
                os.path.expandvars(
                    os.path.expanduser(os.getenv("RETRIEVAL_FASTEMBED_CACHE", "~/.cache/fastembed"))
                )
            ).resolve(),
            hermes_command=os.getenv("HERMES_COMMAND", "hermes"),
            hermes_session_newer_than=os.getenv("HERMES_SESSION_NEWER_THAN", "3650d"),
            max_skills_per_load=max(1, _int_env("RETRIEVAL_MAX_SKILLS_PER_LOAD", 6)),
            max_skill_chars=max(1000, _int_env("RETRIEVAL_MAX_SKILL_CHARS", 16000)),
            max_total_skill_chars=max(2000, _int_env("RETRIEVAL_MAX_TOTAL_SKILL_CHARS", 50000)),
            max_recall_chars=max(1000, _int_env("RETRIEVAL_MAX_RECALL_CHARS", 8000)),
        )

    def sources(self) -> list[SourceConfig]:
        if not self.sources_file.exists():
            raise FileNotFoundError(
                f"{self.sources_file} is missing; copy sources.example.toml and review it"
            )
        payload = tomllib.loads(self.sources_file.read_text(encoding="utf-8"))
        out: list[SourceConfig] = []
        seen: set[str] = set()
        for row in payload.get("sources", []):
            name = str(row["name"]).strip()
            kind = str(row["kind"]).strip()
            if not name or name in seen:
                raise ValueError(f"source names must be non-empty and unique: {name!r}")
            if kind not in {"skills", "context_mode", "hermes_sessions", "librarian"}:
                raise ValueError(f"unsupported source kind {kind!r} for {name}")
            raw_path = os.path.expandvars(os.path.expanduser(str(row["path"])))
            out.append(
                SourceConfig(
                    name=name,
                    kind=kind,
                    path=Path(raw_path).resolve(),
                    enabled=bool(row.get("enabled", True)),
                )
            )
            seen.add(name)
        return out

